"""Scheduler-independent, zero-model title ingestion orchestration."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .classifier import classify_recruitment_title
from .connector import WechatArticleConnector
from .discovery.base import DiscoveryProviderUnavailable, WechatDiscoveryProvider
from .models import DiscoveredArticle
from .normalizer import normalize_wechat_url
from .repository import WechatTitleRepository, queries_for
from ..repository import utc_now
from ...memory_observability import log_memory_checkpoint

logger = logging.getLogger(__name__)
DISCOVERY_NOTICE = '自动搜索发现目前没有可靠的零成本 provider，因此暂时使用 manual discovery。Seed 只是一篇文章，不是公众号文章列表。'


class DiscoveryCooldown(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__('公众号公开搜索仍在冷却期。')


class WechatTitleService:
    def __init__(self, repository: WechatTitleRepository, *,
                 parser: Callable[..., Awaitable[Any]] | None = None,
                 connector: WechatArticleConnector | None = None):
        self.repository = repository
        self.connector = connector or WechatArticleConnector()
        self.parser = parser or self.connector.parse
        self._discovery_lock = asyncio.Lock()
        self._scan_start_lock = asyncio.Lock()
        self._scan_task: asyncio.Task | None = None

    async def start_discovery(self, provider: WechatDiscoveryProvider, *, trigger_type: str = 'manual',
                              respect_cooldown: bool = True) -> dict:
        """Persist a scan run and return immediately; one process owns the scan."""
        async with self._scan_start_lock:
            active = await asyncio.to_thread(self.repository.active_scan_run)
            if active:
                return {'scan_id': active['id'], 'status': 'already_running'}
            sources = [item for item in await asyncio.to_thread(self.repository.list_sources) if item['enabled']]
            rss = log_memory_checkpoint(logger, 'wechat_scan_run', 'queued', trigger_type=trigger_type)
            scan_id = 'wechat-scan-' + uuid.uuid4().hex
            await asyncio.to_thread(self.repository.create_scan_run, scan_id, trigger_type,
                                    total_sources=len(sources), start_rss_mb=rss)
            self._scan_task = asyncio.create_task(
                self._run_discovery(scan_id, provider, respect_cooldown=respect_cooldown),
                name=scan_id,
            )
            return {'scan_id': scan_id, 'status': 'running'}

    async def _run_discovery(self, scan_id: str, provider: WechatDiscoveryProvider, *, respect_cooldown: bool) -> None:
        started = time.monotonic()
        try:
            result = await self.discover_now(provider, respect_cooldown=respect_cooldown, scan_id=scan_id)
            counts = result.get('counts', {})
            status = 'success' if result.get('status') == 'available' else 'partial'
            if result.get('status') in {'unavailable', 'degraded'}:
                status = 'failed' if not result.get('accounts') else 'partial'
            await asyncio.to_thread(self.repository.update_scan_run, scan_id,
                status=status, finished_at=utc_now(), current_source=None,
                raw_candidates=counts.get('raw_candidates', 0),
                deduplicated_candidates=counts.get('deduplicated_candidates', 0),
                accepted_articles=counts.get('discovered', 0), new_articles=counts.get('new', 0),
                leads_created=counts.get('leads', 0), duplicates=counts.get('duplicate', 0),
                failed_count=counts.get('failed', 0),
                error_message=None if status == 'success' else result.get('notice'),
                end_rss_mb=log_memory_checkpoint(logger, 'wechat_scan_run', 'finished', duration_ms=round((time.monotonic()-started)*1000)),
            )
        except Exception as exc:
            await asyncio.to_thread(self.repository.update_scan_run, scan_id,
                status='failed', finished_at=utc_now(), current_source=None, failed_count=1,
                error_message='公开搜索本轮未完成。',
                end_rss_mb=log_memory_checkpoint(logger, 'wechat_scan_run', 'failed'),
            )
            logger.exception('wechat_scan_run_failed', extra={'scan_id': scan_id})

    async def import_article(self, url: str, *, expected_source_name: str | None = None,
                             force_refresh: bool = False) -> dict:
        started = time.monotonic()
        try:
            normalized = normalize_wechat_url(url)
        except ValueError:
            return {'url': url[:2048], 'fetch_status': 'unsafe_url', 'error': '只允许有效的 HTTPS 微信公开文章 URL。',
                    'is_new': False, 'lead_created': False, 'verification_status': 'unverified'}
        existing = await asyncio.to_thread(self.repository.get_by_url, normalized)
        if existing and existing['fetch_status'] == 'success' and not force_refresh:
            result = {**existing, 'is_new': False, 'lead_created': False, 'cached': True}
        else:
            metadata = await self.parser(normalized, expected_source_name=expected_source_name)
            data = metadata.model_dump(mode='json')
            data['requested_url'] = normalized
            if existing and data['fetch_status'] != 'success':
                # A failing original short URL must update the known canonical
                # document instead of creating another failed alias document.
                data.update(normalized_url=existing['normalized_url'], url=existing['url'])
            classification = classify_recruitment_title(data.get('title') or '').model_dump(mode='json')
            result = await asyncio.to_thread(self.repository.save, data, classification, expected_source_name)
        logger.info('wechat_title_ingest', extra={
            'source': result.get('source_name'), 'article_url': normalized,
            'fetch_result': result.get('fetch_status'), 'parse_result': bool(result.get('title')),
            'new_article': result.get('is_new'), 'duplicate': not result.get('is_new'),
            'relevance_score': result.get('relevance_score'), 'lead_created': result.get('lead_created'),
            'duration_ms': round((time.monotonic() - started) * 1000),
        })
        return result

    async def import_batch(self, urls: list[str], *, expected_source_name: str | None = None,
                           force_refresh: bool = False) -> dict:
        memory_before = log_memory_checkpoint(
            logger, "wechat_batch_import", "before", item_count=len(urls),
        )
        # Five independent requests maximum; equivalent URLs share one task.
        semaphore = asyncio.Semaphore(5)
        async def one(url: str) -> dict:
            async with semaphore:
                try:
                    return await self.import_article(url, expected_source_name=expected_source_name,
                                                     force_refresh=force_refresh)
                except Exception:
                    logger.exception('wechat_title_item_failed')
                    return {'url': url[:2048], 'fetch_status': 'storage_or_processing_error',
                            'error': '此条处理失败，可单独重试；其他条目继续保存。',
                            'is_new': False, 'lead_created': False}
        tasks: dict[str, asyncio.Task] = {}
        keys = []
        for url in urls:
            try:
                key = normalize_wechat_url(url)
            except ValueError:
                key = url
            keys.append(key)
            if key not in tasks:
                tasks[key] = asyncio.create_task(one(url))
        await asyncio.gather(*tasks.values())
        results, seen = [], set()
        for key in keys:
            result = dict(tasks[key].result())
            if key in seen:
                result.update(is_new=False, lead_created=False, cached=True)
            seen.add(key)
            results.append(result)
        successful = [item for item in results if item.get('fetch_status') == 'success']
        result = {'total': len(results), 'success': len(successful),
                  'new': sum(bool(item['is_new']) for item in successful),
                  'duplicate': sum(not item['is_new'] for item in successful),
                  'failed': len(results) - len(successful), 'items': results,
                  'ai_calls': 0, 'model_tokens_used': 0}
        log_memory_checkpoint(
            logger, "wechat_batch_import", "after", before_mb=memory_before,
            item_count=len(urls), success=result['success'],
        )
        return result

    async def import_watchlist_seeds(self, *, force_refresh: bool = False) -> dict:
        """Import every enabled configured seed with its account provenance.

        This is an explicit user action. A seed is one known historical article,
        not an account feed and not automatic discovery of later publications.
        """
        sources = await asyncio.to_thread(self.repository.list_sources)
        configured = [
            source for source in sources
            if source.get('enabled') and source.get('seed_url')
        ]
        semaphore = asyncio.Semaphore(5)

        async def one(source: dict) -> dict:
            async with semaphore:
                return await self.import_article(
                    source['seed_url'], expected_source_name=source['source_name'],
                    force_refresh=force_refresh,
                )

        results = await asyncio.gather(*(one(source) for source in configured))
        successful = [item for item in results if item.get('fetch_status') == 'success']
        return {
            'total': len(results), 'success': len(successful),
            'new': sum(bool(item.get('is_new')) for item in successful),
            'duplicate': sum(not item.get('is_new') for item in successful),
            'failed': len(results) - len(successful), 'items': results,
            'scope': 'configured_watchlist_seeds',
            'notice': '已导入观察名单中的已知历史文章入口；这不代表已发现公众号后续新文章。',
            'ai_calls': 0, 'model_tokens_used': 0,
        }

    async def discover_now(self, provider: WechatDiscoveryProvider, *, respect_cooldown: bool = True,
                           scan_id: str | None = None) -> dict:
        provider_name = getattr(provider, 'name', provider.__class__.__name__)
        if self._discovery_lock.locked():
            return {
                'status': 'already_running', 'provider': provider_name,
                'accounts': [], 'counts': {},
                'notice': '公众号公开搜索已有一轮正在运行；本次未重复启动。',
                'ai_calls': 0, 'model_tokens_used': 0,
            }
        memory_before = log_memory_checkpoint(
            logger, "wechat_discovery", "before", provider=provider_name,
        )
        async with self._discovery_lock:
            try:
                return await self._discover_now_unlocked(
                    provider, respect_cooldown=respect_cooldown, scan_id=scan_id,
                )
            finally:
                log_memory_checkpoint(
                    logger, "wechat_discovery", "after", before_mb=memory_before,
                    provider=provider_name,
                )

    async def _discover_now_unlocked(
        self, provider: WechatDiscoveryProvider, *, respect_cooldown: bool = True,
        scan_id: str | None = None,
    ) -> dict:
        provider_name = getattr(provider, 'name', provider.__class__.__name__)
        state = await asyncio.to_thread(self.repository.get_provider_state, provider_name)
        if respect_cooldown and state.get('cooldown_until'):
            from datetime import datetime, timezone
            try:
                remaining = int((datetime.fromisoformat(state['cooldown_until']) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                remaining = 0
            if remaining > 0:
                raise DiscoveryCooldown(remaining)
        sources = [source for source in await asyncio.to_thread(self.repository.list_sources) if source['enabled']]
        counts = {'discovered': 0, 'raw_candidates': 0, 'deduplicated_candidates': 0,
                  'source_matched': 0, 'new': 0, 'duplicate': 0, 'related': 0, 'failed': 0,
                  'leads': 0,
                  'accounts_scanned': 0, 'cached_accounts': 0}
        accounts: list[dict] = []
        try:
            for source in sources:
                if scan_id:
                    await asyncio.to_thread(self.repository.update_scan_run, scan_id,
                                            current_source=source['source_name'], completed_sources=counts['accounts_scanned'])
                candidates: list[dict] = []
                discovered: list[DiscoveredArticle] = []
                executed_queries, cached_queries = [], 0
                for query in queries_for(source['source_name']):
                    # Cache by account + exact query.  A parser revision is in
                    # the key so an old single-query empty cache is never used.
                    query_key = source['source_name'].strip().casefold() + '|sogou-v3|' + query.casefold()
                    cached = await asyncio.to_thread(self.repository.cache_get, provider_name, source['id'], query_key)
                    if cached is None:
                        if hasattr(provider, 'discover_with_debug'):
                            query_items, query_candidates = await provider.discover_with_debug(source, query=query)
                        else:
                            query_items, query_candidates = await provider.discover(source), []
                        payload = {'items': [item.model_dump(mode='json') for item in query_items],
                                   'candidates': [self._serialize_candidate(row) for row in query_candidates]}
                        await asyncio.to_thread(self.repository.cache_set, provider_name, source['id'], query_key, payload)
                    else:
                        payload = cached if isinstance(cached, dict) else {'items': cached, 'candidates': []}
                        query_items = [DiscoveredArticle.model_validate(item) for item in payload.get('items', [])]
                        query_candidates = payload.get('candidates', [])
                        cached_queries += 1
                    executed_queries.append(query)
                    candidates.extend(query_candidates)
                    discovered.extend(query_items)
                candidates, discovered = self._merge_query_results(candidates, discovered)
                counts['raw_candidates'] += len(candidates)
                counts['deduplicated_candidates'] += sum(1 for item in candidates if not item.get('duplicate_of'))
                counts['source_matched'] += sum(1 for item in candidates if item.get('rejection_reason') in {'accepted', 'resolve_failed', 'duplicate'})
                if cached_queries:
                    counts['cached_accounts'] += 1
                account = {'source_name': source['source_name'], 'queries': executed_queries,
                           'raw_candidates': len(candidates),
                           'deduplicated_candidates': sum(1 for item in candidates if not item.get('duplicate_of')),
                           'source_matched': sum(1 for item in candidates if item.get('rejection_reason') in {'accepted', 'resolve_failed', 'duplicate'}),
                           'discovered': len(discovered), 'new': 0, 'duplicate': 0, 'related': 0, 'leads': 0,
                           'cached': cached_queries == len(executed_queries), 'cached_queries': cached_queries}
                counts['accounts_scanned'] += 1
                counts['discovered'] += len(discovered)
                for found in discovered:
                    direct = found.article_url
                    result = None
                    if direct:
                        result = await self.import_article(direct, expected_source_name=source['source_name'])
                    if not result or result.get('fetch_status') != 'success':
                        data = found.model_dump(mode='json')
                        classification = classify_recruitment_title(found.title or '').model_dump(mode='json')
                        result = await asyncio.to_thread(
                            self.repository.save_discovery, data, classification, source['source_name'],
                        )
                    key = 'new' if result.get('is_new') else 'duplicate'
                    # Candidate debug describes the final outcome too.  A
                    # deduplicated accepted result is not a fresh lead.
                    for candidate in candidates:
                        if candidate.get('discovery_url') == found.discovery_url and not result.get('is_new'):
                            candidate['accepted'] = False
                            candidate['rejection_reason'] = 'duplicate'
                    account[key] += 1
                    counts[key] += 1
                    if result.get('relevance_status') in {'relevant', 'possible'}:
                        account['related'] += 1
                        counts['related'] += 1
                    if result.get('lead_created'):
                        account['leads'] += 1
                        counts['leads'] += 1
                if candidates:
                    # Save a second bounded snapshot only when final ingest
                    # changed a decision (typically duplicate).  This keeps
                    # diagnosis accurate without retaining response bodies.
                    await asyncio.to_thread(
                        self.repository.save_discovery_debug, provider_name, source, candidates,
                    )
                accounts.append(account)
                if scan_id:
                    await asyncio.to_thread(self.repository.update_scan_run, scan_id,
                        completed_sources=counts['accounts_scanned'], raw_candidates=counts['raw_candidates'],
                        deduplicated_candidates=counts['deduplicated_candidates'], accepted_articles=counts['discovered'],
                        new_articles=counts['new'], leads_created=counts['leads'], duplicates=counts['duplicate'],
                        failed_count=counts['failed'])
        except DiscoveryProviderUnavailable as exc:
            counts['failed'] += 1
            provider_state = await asyncio.to_thread(
                self.repository.set_provider_state, provider_name, status='unavailable',
                counts=counts, failure_reason=str(exc),
            )
            return {'status': 'unavailable', 'provider': provider_name, 'accounts': accounts,
                    'counts': counts, 'provider_state': provider_state,
                    'notice': '公开搜索暂不可用；手动 URL 导入仍可正常使用。',
                    'ai_calls': 0, 'model_tokens_used': 0}
        except Exception:
            logger.exception('wechat_discovery_failed', extra={'provider': provider_name})
            counts['failed'] += 1
            provider_state = await asyncio.to_thread(
                self.repository.set_provider_state, provider_name, status='degraded',
                counts=counts, failure_reason='公开搜索本轮处理失败。',
            )
            return {'status': 'degraded', 'provider': provider_name, 'accounts': accounts,
                    'counts': counts, 'provider_state': provider_state,
                    'notice': '公开搜索本轮未完成；手动 URL 导入仍可正常使用。',
                    'ai_calls': 0, 'model_tokens_used': 0}
        provider_state = await asyncio.to_thread(
            self.repository.set_provider_state, provider_name, status='available', counts=counts,
        )
        return {'status': 'available', 'provider': provider_name, 'accounts': accounts,
                'counts': counts, 'provider_state': provider_state,
                'ai_calls': 0, 'model_tokens_used': 0}

    @staticmethod
    def _serialize_candidate(candidate: dict) -> dict:
        value = dict(candidate)
        published = value.get('published_at')
        if hasattr(published, 'isoformat'):
            value['published_at'] = published.isoformat()
        return value

    @staticmethod
    def _candidate_key(candidate: dict) -> str:
        if candidate.get('resolved_wechat_url'):
            return 'wechat:' + candidate['resolved_wechat_url']
        # Sogou tokens vary by query, so the raw result URL is only a fallback.
        return '|'.join(('meta', candidate.get('normalized_source_name') or '',
                         (candidate.get('raw_title') or '').strip().casefold(),
                         str(candidate.get('published_at') or '')[:25]))

    @classmethod
    def _merge_query_results(cls, candidates: list[dict], items: list[DiscoveredArticle]) -> tuple[list[dict], list[DiscoveredArticle]]:
        masters: dict[str, dict] = {}
        for candidate in candidates:
            candidate.setdefault('found_by_queries', [candidate.get('query')] if candidate.get('query') else [])
            key = cls._candidate_key(candidate)
            existing = masters.get(key)
            if existing is None:
                masters[key] = candidate
                continue
            existing['found_by_queries'] = list(dict.fromkeys(existing['found_by_queries'] + candidate['found_by_queries']))
            candidate['found_by_queries'] = existing['found_by_queries']
            candidate['duplicate_of'] = key
            if candidate.get('accepted'):
                candidate['accepted'] = False
                candidate['rejection_reason'] = 'duplicate'
        merged: dict[str, DiscoveredArticle] = {}
        for item in items:
            candidate_key = '|'.join(('meta', (item.source_name or '').strip().casefold(),
                                      (item.title or '').strip().casefold(), str(item.published_at or '')[:25]))
            existing = merged.get(candidate_key)
            if existing is None:
                merged[candidate_key] = item
            else:
                existing.found_by_queries = list(dict.fromkeys(existing.found_by_queries + item.found_by_queries))
        return candidates, list(merged.values())


async def run_wechat_monitor(service: WechatTitleService, provider: WechatDiscoveryProvider | None = None) -> dict:
    """Call from an existing scheduler when a lawful discovery provider is configured.

    With no provider, do not fetch seeds and pretend to monitor new publications.
    Manual URL imports use the same service directly. No scheduler starts by import.
    """
    sources = await asyncio.to_thread(service.repository.list_sources)
    if provider is None:
        return {'status': 'provider_pending', 'notice': DISCOVERY_NOTICE, 'accounts': sources,
                'ai_calls': 0, 'model_tokens_used': 0}
    results = []
    for source in sources:
        if not source['enabled']:
            continue
        try:
            discovered = await provider.discover(source)
            urls = [item.url if hasattr(item, 'url') else item['url'] for item in discovered]
            account_result = {'source_name': source['source_name'], 'total': 0, 'success': 0,
                              'new': 0, 'duplicate': 0, 'failed': 0, 'items': []}
            for offset in range(0, len(urls), 50):
                batch = await service.import_batch(urls[offset:offset + 50], expected_source_name=source['source_name'])
                for key in ('total', 'success', 'new', 'duplicate', 'failed'):
                    account_result[key] += batch[key]
                account_result['items'].extend(batch['items'])
            account_result['status'] = 'partial' if account_result['failed'] else 'completed'
            results.append(account_result)
        except Exception:
            logger.exception('wechat_discovery_failed', extra={'source': source['source_name']})
            results.append({'source_name': source['source_name'], 'status': 'discovery_failed'})
    incomplete = any(item['status'] in {'partial', 'discovery_failed'} for item in results)
    return {'status': 'partial' if incomplete else 'completed', 'accounts': results, 'ai_calls': 0, 'model_tokens_used': 0}
