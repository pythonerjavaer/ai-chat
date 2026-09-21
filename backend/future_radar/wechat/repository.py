"""Title metadata and unverified leads, using the existing Radar database."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from ..repository import RadarRepository, utc_now
from .normalizer import metadata_fingerprint, normalize_wechat_url
from .discovery.search import build_discovery_queries

WATCHLIST = (
    ('国央校招', 'https://mp.weixin.qq.com/s/2YRjkdejWz-AAOcpoS85WA'),
    ('国聘', 'https://mp.weixin.qq.com/s/uXDQ9RBPBcIRAjZV7S2jgg'),
    ('国资小新', 'https://mp.weixin.qq.com/s/fKmsPl5K33HLDk-kUKOZEw'),
    ('国央求职网', 'https://mp.weixin.qq.com/s/CqdDhYs-gP6l1J4ipjWSyQ'),
    ('银行招聘网', 'https://mp.weixin.qq.com/s/1fuJNWMpHT7Oiq0kD3poFA'),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def account_id(name: str) -> str:
    return 'wechat-title-' + hashlib.sha256(name.strip().encode()).hexdigest()[:24]


def queries_for(name: str) -> list[str]:
    # Persisted alongside the source, even while the search provider is pending.
    return build_discovery_queries(name)


def public_article(row: dict) -> dict:
    return {
        'id': row['id'], 'platform': 'wechat', 'source_name': row['publisher'] or None,
        'title': row['article_title'] or None, 'url': row['article_url'],
        'normalized_url': row['normalized_url'], 'published_at': row['publish_time'],
        'discovered_at': row['first_seen_at'], 'updated_at': row['updated_at'],
        'fetch_status': row['fetch_status'], 'error': row['fetch_error'],
        'relevance_status': row['classification'], 'relevance_score': row['relevance_score'],
        'matched_keywords': json.loads(row['matched_keywords'] or '[]'),
        'source_name_detection': row['source_name_detection'],
        'metadata_fingerprint': row['metadata_fingerprint'],
        'verification_status': 'unverified', 'lead_id': row.get('lead_id'),
        'related_title_count': int(row.get('related_title_count') or 0),
    }


class WechatTitleRepository(RadarRepository):
    """Reuses source/account storage without enrolling these sources in old scans."""

    def _ensure_source(self, connection: Any, name: str, seed: str | None,
                       *, watch: bool, enabled: bool = True, update: bool = False) -> str:
        source_id, now = account_id(name), utc_now()
        connection.execute('''
            INSERT INTO monitor_sources
              (id,name,platform,source_type,url,domain,account_name,enabled,
               adapter_config,query_config,status,verification_status,
               title_radar_account,title_radar_enabled,created_at,updated_at)
            VALUES (?,?, 'wechat','wechat_public',?,'mp.weixin.qq.com',?,0,?,?,'manual','unverified',?,?,?,?)
            ON CONFLICT(id) DO NOTHING
        ''', (source_id, name, seed, name, _json({'adapter': 'wechat_title_metadata'}),
              _json({'discovery_method': 'manual', 'queries': queries_for(name)}),
              int(watch), int(enabled and watch), now, now))
        if update:
            connection.execute('''UPDATE monitor_sources SET title_radar_account=1,
                title_radar_enabled=?,url=?,updated_at=? WHERE id=?''',
                (int(enabled), seed, now, source_id))
        return source_id

    def seed_watchlist(self) -> None:
        with self.transaction() as connection:
            for name, seed in WATCHLIST:
                self._ensure_source(connection, name, seed, watch=True)

    def add_source(self, name: str, seed: str | None, enabled: bool) -> dict:
        seed = normalize_wechat_url(seed) if seed else None
        with self.transaction() as connection:
            source_id = self._ensure_source(connection, name.strip(), seed,
                                            watch=True, enabled=enabled, update=True)
        return next(item for item in self.list_sources() if item['id'] == source_id)

    def list_sources(self) -> list[dict]:
        recent = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with closing(self._connect()) as connection:
            rows = connection.execute('''SELECT s.*,
                (SELECT COUNT(*) FROM source_articles a WHERE a.source_id=s.id AND a.platform='wechat') total_articles,
                (SELECT COUNT(*) FROM source_articles a WHERE a.source_id=s.id AND a.platform='wechat'
                 AND a.fetch_status='success' AND a.first_seen_at>=?) new_articles
                FROM monitor_sources s WHERE s.title_radar_account=1 ORDER BY s.created_at,s.name''', (recent,)).fetchall()
        return [{
            'id': row['id'], 'platform': 'wechat', 'source_name': row['name'],
            'seed_url': row['url'], 'enabled': bool(row['title_radar_enabled']),
            'discovery_method': 'manual', 'discovery_status': 'provider_pending',
            'discovery_queries': json.loads(row['query_config']).get('queries', []),
            'last_checked_at': row['last_checked_at'], 'last_status': row['status'],
            'total_articles': row['total_articles'],
            'new_articles': row['new_articles'],
        } for row in rows]

    def get_by_url(self, url: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute('''SELECT a.*, l.id lead_id FROM source_articles a
                LEFT JOIN recruitment_title_leads l ON l.source_document_id=a.id
                WHERE a.platform='wechat' AND (a.normalized_url=? OR a.id IN
                  (SELECT source_document_id FROM wechat_article_url_aliases WHERE normalized_url=?))''', (url, url)).fetchone()
        return public_article(dict(row)) if row else None

    def save(self, metadata: dict, classification: dict, expected_source: str | None = None) -> dict:
        url, now = metadata['normalized_url'], utc_now()
        article_id = 'wechat-article-' + hashlib.sha256(url.encode()).hexdigest()[:32]
        with self.transaction() as connection:
            existing = connection.execute('''SELECT * FROM source_articles
                WHERE platform='wechat' AND normalized_url=?''', (url,)).fetchone()
            redirected = None
            requested_url = metadata.get('requested_url') or url
            if requested_url != url and metadata['fetch_status'] == 'success':
                redirected = connection.execute('''SELECT a.* FROM source_articles a
                    WHERE a.platform='wechat' AND (a.normalized_url=? OR a.id IN
                      (SELECT source_document_id FROM wechat_article_url_aliases WHERE normalized_url=?))''',
                    (requested_url, requested_url)).fetchone()
                if not existing and redirected:
                    existing = redirected
            previous = dict(existing) if existing else {}
            if existing:
                article_id = existing['id']
            if redirected:
                previous['first_seen_at'] = min(previous.get('first_seen_at') or now, redirected['first_seen_at'])
                previous['created_at'] = min(previous.get('created_at') or now, redirected['created_at'])
                previous['publish_time'] = previous.get('publish_time') or redirected['publish_time']
                if previous.get('source_name_detection') != 'page' and redirected['source_name_detection'] == 'page':
                    previous.update(publisher=redirected['publisher'], source_name_detection='page')
            # A failed refresh may update diagnostics but never erase known facts.
            successful = metadata['fetch_status'] == 'success'
            title = (metadata.get('title') if successful else None) or previous.get('article_title', '')
            publisher = metadata.get('source_name') or previous.get('publisher') or ''
            published = metadata.get('published_at') or previous.get('publish_time')
            detection = metadata.get('source_name_detection', 'unknown')
            if previous.get('source_name_detection') == 'page' and (not successful or detection != 'page'):
                publisher, detection = previous['publisher'], 'page'
            if detection == 'unknown' and previous:
                detection = previous['source_name_detection']
            if not successful and previous:
                classification = {'relevance_status': previous['classification'],
                                  'relevance_score': previous['relevance_score'],
                                  'matched_keywords': json.loads(previous['matched_keywords'])}
            source_id = self._ensure_source(connection, publisher or expected_source or '未识别公众号',
                                            None, watch=False)
            fingerprint = metadata_fingerprint(publisher, title) if title else None
            semantic = _json([title, publisher, published, classification, detection])
            content_hash = hashlib.sha256(semantic.encode()).hexdigest()
            values = (article_id, source_id, article_id, publisher, title, url, published,
                      content_hash, int(classification['relevance_status'] in ('relevant', 'possible')),
                      classification['relevance_status'], previous.get('first_seen_at') or now,
                      now, previous.get('created_at') or now, url, metadata['fetch_status'],
                      metadata.get('error'), classification['relevance_score'],
                      _json(classification['matched_keywords']), detection, fingerprint, now)
            connection.execute('''INSERT INTO source_articles
                (id,source_id,article_external_id,publisher,article_title,article_url,publish_time,
                 content_hash,is_recruitment,classification,first_seen_at,last_seen_at,created_at,
                 platform,normalized_url,fetch_status,fetch_error,relevance_score,matched_keywords,
                 source_name_detection,metadata_fingerprint,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'wechat',?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,publisher=excluded.publisher,
                  normalized_url=excluded.normalized_url,article_url=excluded.article_url,
                  article_title=excluded.article_title,publish_time=excluded.publish_time,
                  content_hash=excluded.content_hash,is_recruitment=excluded.is_recruitment,
                  classification=excluded.classification,last_seen_at=excluded.last_seen_at,
                  fetch_status=excluded.fetch_status,fetch_error=excluded.fetch_error,
                  relevance_score=excluded.relevance_score,matched_keywords=excluded.matched_keywords,
                  source_name_detection=excluded.source_name_detection,
                  metadata_fingerprint=excluded.metadata_fingerprint,updated_at=excluded.updated_at,
                  first_seen_at=excluded.first_seen_at,created_at=excluded.created_at
            ''', values)
            if redirected and redirected['id'] != article_id:
                connection.execute('''UPDATE wechat_article_url_aliases SET source_document_id=?
                    WHERE source_document_id=?''', (article_id, redirected['id']))
                connection.execute('DELETE FROM source_articles WHERE id=?', (redirected['id'],))
            for alias in {url, metadata.get('requested_url') or url}:
                connection.execute('''INSERT INTO wechat_article_url_aliases(normalized_url,source_document_id)
                    VALUES (?,?) ON CONFLICT(normalized_url) DO UPDATE SET source_document_id=excluded.source_document_id''',
                    (alias, article_id))
            lead_id = 'title-lead-' + article_id.removeprefix('wechat-article-')
            lead_created = False
            if title and classification['relevance_status'] in ('relevant', 'possible'):
                lead_created = not bool(connection.execute(
                    'SELECT id FROM recruitment_title_leads WHERE source_document_id=?', (article_id,)
                ).fetchone())
                connection.execute('''INSERT INTO recruitment_title_leads
                    (id,source_document_id,title,source_name,source_url,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_document_id) DO UPDATE SET
                    title=excluded.title,source_name=excluded.source_name,source_url=excluded.source_url,
                    updated_at=excluded.updated_at
                ''', (lead_id, article_id, title, publisher or None, url, now, now))
            elif successful:
                connection.execute("DELETE FROM recruitment_title_leads WHERE source_document_id=? AND status='unverified'",
                                   (article_id,))
            connection.execute('''UPDATE monitor_sources SET last_checked_at=?,status=?,updated_at=? WHERE id=?''',
                               (now, metadata['fetch_status'], now, source_id))
            if expected_source and account_id(expected_source) != source_id:
                connection.execute('''UPDATE monitor_sources SET last_checked_at=?,status=?,updated_at=? WHERE id=?''',
                                   (now, metadata['fetch_status'], now, account_id(expected_source)))
        result = self.get_by_url(url)
        return {**result, 'is_new': not bool(existing), 'lead_created': lead_created, 'cached': False}

    def list_articles(self, *, page: int = 1, page_size: int = 30,
                      source_name: str | None = None, relevance_status: str | None = None,
                      from_date: str | None = None, to_date: str | None = None) -> dict:
        predicates, params = ["a.platform='wechat'"], []
        for condition, value in (
            ('a.publisher=?', source_name), ('a.classification=?', relevance_status),
            ('SUBSTR(a.publish_time,1,10)>=?', from_date), ('SUBSTR(a.publish_time,1,10)<=?', to_date),
        ):
            if value:
                predicates.append(condition)
                params.append(value)
        where = ' AND '.join(predicates)
        with closing(self._connect()) as connection:
            total = connection.execute(f'SELECT COUNT(*) count FROM source_articles a WHERE {where}', params).fetchone()['count']
            rows = connection.execute(f'''SELECT a.*, l.id lead_id,
                CASE WHEN a.metadata_fingerprint IS NULL THEN 0 ELSE
                (SELECT COUNT(*)-1 FROM source_articles b WHERE b.platform='wechat'
                 AND b.metadata_fingerprint=a.metadata_fingerprint) END related_title_count
                FROM source_articles a LEFT JOIN recruitment_title_leads l ON l.source_document_id=a.id
                WHERE {where} ORDER BY a.first_seen_at DESC,a.id LIMIT ? OFFSET ?''',
                [*params, page_size, (page - 1) * page_size]).fetchall()
        return {'items': [public_article(dict(row)) for row in rows],
                'total': total, 'page': page, 'page_size': page_size}
