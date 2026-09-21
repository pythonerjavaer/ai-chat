"""Additive SQLite/PostgreSQL migration; no legacy article backfill or body copy."""
from typing import Any, Callable


def migrate_wechat_titles(connection: Any, ensure_column: Callable) -> None:
    ensure_column(connection, 'monitor_sources', 'title_radar_account', 'INTEGER NOT NULL DEFAULT 0')
    ensure_column(connection, 'monitor_sources', 'title_radar_enabled', 'INTEGER NOT NULL DEFAULT 0')
    for name, declaration in {
        'platform': 'TEXT',
        'normalized_url': 'TEXT',
        'fetch_status': 'TEXT',
        'fetch_error': 'TEXT',
        'relevance_score': 'INTEGER NOT NULL DEFAULT 0',
        'matched_keywords': "TEXT NOT NULL DEFAULT '[]'",
        'source_name_detection': "TEXT NOT NULL DEFAULT 'unknown'",
        'metadata_fingerprint': 'TEXT',
        'updated_at': 'TEXT',
    }.items():
        ensure_column(connection, 'source_articles', name, declaration)
    connection.executescript('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wechat_article_url
            ON source_articles(platform, normalized_url) WHERE normalized_url IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_wechat_article_fingerprint
            ON source_articles(metadata_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_wechat_article_listing
            ON source_articles(platform, first_seen_at DESC);
        CREATE TABLE IF NOT EXISTS recruitment_title_leads (
            id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL DEFAULT 'wechat',
            title TEXT NOT NULL,
            source_name TEXT,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unverified',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (source_document_id) REFERENCES source_articles(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS wechat_article_url_aliases (
            normalized_url TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL,
            FOREIGN KEY (source_document_id) REFERENCES source_articles(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS wechat_discovery_provider_state (
            provider TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'unavailable',
            last_scan_at TEXT,
            last_success_at TEXT,
            last_failure_at TEXT,
            failure_reason TEXT,
            cooldown_until TEXT,
            last_counts TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wechat_discovery_cache (
            provider TEXT NOT NULL,
            source_id TEXT NOT NULL,
            query_key TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, source_id, query_key),
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_wechat_discovery_cache_expiry
            ON wechat_discovery_cache(expires_at);
        CREATE TABLE IF NOT EXISTS wechat_discovery_debug (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            scanned_at TEXT NOT NULL,
            raw_title TEXT,
            raw_source_name TEXT,
            normalized_source_name TEXT,
            raw_date TEXT,
            published_at TEXT,
            discovery_url TEXT,
            resolved_wechat_url TEXT,
            accepted INTEGER NOT NULL DEFAULT 0,
            rejection_reason TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES monitor_sources(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_wechat_discovery_debug_recent
            ON wechat_discovery_debug(provider, scanned_at DESC);
    ''')
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
        ('future_radar_v5_wechat_titles',),
    )
