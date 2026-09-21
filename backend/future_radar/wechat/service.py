"""Scheduler-independent, zero-model title ingestion orchestration."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .classifier import classify_recruitment_title
from .connector import WechatArticleConnector
from .discovery.base import WechatDiscoveryProvider
from .normalizer import normalize_wechat_url
from .repository import WechatTitleRepository

logger = logging.getLogger(__name__)
DISCOVERY_NOTICE = '自动搜索发现目前没有可靠的零成本 provider，因此暂时使用 manual discovery。Seed 只是一篇文章，不是公众号文章列表。'


class WechatTitleService:
    def __init__(self, repository: WechatTitleRepository, *,
                 parser: Callable[..., Awaitable[Any]] | None = None,
                 connector: WechatArticleConnector | None = None):
        self.repository = repository
        self.connector = connector or WechatArticleConnector()
        self.parser = parser or self.connector.parse

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
        return {'total': len(results), 'success': len(successful),
                'new': sum(bool(item['is_new']) for item in successful),
                'duplicate': sum(not item['is_new'] for item in successful),
                'failed': len(results) - len(successful), 'items': results,
                'ai_calls': 0, 'model_tokens_used': 0}

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
