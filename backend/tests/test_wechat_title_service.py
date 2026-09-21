"""Offline persistence, orchestration and HTTP contracts for title-only leads."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import os
import sqlite3

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "wechat-title-fixture-secret-at-least-32-characters")

from backend.future_radar.repository import RadarRepository
from backend.future_radar.schema import migrate
from backend.future_radar.seeds import initial_sources
from backend.future_radar.wechat.models import WechatArticleMetadata
from backend.future_radar.wechat.normalizer import normalize_wechat_url
from backend.future_radar.wechat.repository import WATCHLIST, WechatTitleRepository
from backend.future_radar.wechat.routes import create_wechat_router
from backend.future_radar.wechat.service import DiscoveryCooldown, WechatTitleService, run_wechat_monitor
from backend.future_radar.wechat.discovery.base import DiscoveryProviderUnavailable
from backend.future_radar.wechat.models import DiscoveredArticle


@pytest.fixture
def title_repo(tmp_path):
    path = tmp_path / "wechat-title.sqlite3"

    def connect():
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    with closing(connect()) as connection:
        migrate(connection)
        connection.commit()
    repo = WechatTitleRepository(connect)
    repo.seed_watchlist()
    return repo


def metadata(url, **overrides):
    return WechatArticleMetadata(**{
        "url": url, "normalized_url": normalize_wechat_url(url),
        "source_name": "国聘", "source_name_detection": "page",
        "title": "中国银行2027届校园招聘启动", "fetch_status": "success",
        **overrides,
    })


class Parser:
    def __init__(self, items=None):
        self.items = items or {}
        self.calls = []

    async def __call__(self, url, expected_source_name=None):
        self.calls.append((url, expected_source_name))
        result = self.items.get(url)
        if isinstance(result, Exception):
            raise result
        return result or metadata(url)


def count(repo, table):
    assert table in {"radar_jobs", "recruitment_programs", "source_articles", "recruitment_title_leads"}
    with closing(repo._connect()) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_additive_migration_preserves_legacy_rows_and_seed_preferences(title_repo):
    legacy = RadarRepository(title_repo._connect)
    legacy.seed_sources(initial_sources(web_search_enabled=False))
    legacy.create_source({"id": "legacy-article-fixture", "name": "Legacy fixture", "source_type": "manual"})
    with legacy.transaction() as connection:
        legacy.upsert_article(connection, {
            "article_external_id": "old-article", "publisher": "Legacy publisher",
            "article_title": "Retained legacy announcement", "article_url": "https://example.com/legacy",
            "content_hash": "old-content-hash", "raw_excerpt": "Existing legacy summary retained",
            "classification": "legacy", "is_recruitment": True,
        }, source_id="legacy-article-fixture", now="2026-01-01T00:00:00+00:00")
    title_repo.add_source("国聘", WATCHLIST[1][1], False)
    with closing(title_repo._connect()) as connection:
        before = dict(connection.execute("SELECT * FROM source_articles WHERE article_external_id='old-article'").fetchone())
        migrate(connection)
        migrate(connection)
        connection.commit()
        after = dict(connection.execute("SELECT * FROM source_articles WHERE article_external_id='old-article'").fetchone())
        assert after == before
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='future_radar_v5_wechat_titles'").fetchone()[0] == 1
    title_repo.seed_watchlist()
    sources = title_repo.list_sources()
    assert {source["source_name"] for source in sources} == {name for name, _ in WATCHLIST}
    assert len(sources) == 5
    assert next(source for source in sources if source["source_name"] == "国聘")["enabled"] is False
    assert all(source["discovery_status"] == "unavailable" for source in sources)
    # Both old paid/body sources and new title-only watchlist records stay out
    # of the scheduler/manual Quick or Deep source families.
    enabled = legacy.list_sources(enabled=True)
    assert all(source["source_type"] != "wechat_public" for source in enabled)
    assert title_repo.list_articles()["total"] == 0


def test_discovery_debug_is_bounded_and_keeps_only_scalar_candidate_fields(title_repo):
    source = next(item for item in title_repo.list_sources() if item["source_name"] == "国聘")
    title_repo.save_discovery_debug("sogou_wechat", source, [{
        "raw_title": "国聘 2027 届校园招聘", "raw_source_name": "国聘",
        "normalized_source_name": "国聘", "raw_date": "1789948800",
        "published_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
        "discovery_url": "https://weixin.sogou.com/link?url=x",
        "resolved_wechat_url": None, "accepted": True,
        "rejection_reason": "resolve_failed",
    }])
    saved = title_repo.list_discovery_debug()
    assert saved["items"][0]["raw_title"] == "国聘 2027 届校园招聘"
    assert saved["items"][0]["accepted"] is True
    assert saved["items"][0]["rejection_reason"] == "resolve_failed"


def test_normalized_url_cache_is_idempotent_and_never_creates_fake_jobs(title_repo):
    parser = Parser()
    service = WechatTitleService(title_repo, parser=parser)
    url = "https://mp.weixin.qq.com/s/campus-fixture"
    first = asyncio.run(service.import_article(url + "?scene=1#wechat_redirect"))
    replay = asyncio.run(service.import_article(url + "?utm_source=other"))
    refreshed = asyncio.run(service.import_article(url, force_refresh=True))
    assert first["is_new"] and first["lead_created"]
    assert replay["cached"] and not replay["is_new"] and not replay["lead_created"]
    assert not refreshed["cached"] and not refreshed["is_new"] and not refreshed["lead_created"]
    assert first["id"] == replay["id"] == refreshed["id"]
    assert len(parser.calls) == 2
    assert first["verification_status"] == "unverified"
    assert count(title_repo, "source_articles") == count(title_repo, "recruitment_title_leads") == 1
    assert count(title_repo, "radar_jobs") == count(title_repo, "recruitment_programs") == 0
    with closing(title_repo._connect()) as connection:
        stored = dict(connection.execute("SELECT * FROM source_articles").fetchone())
        assert stored["raw_excerpt"] == ""
        assert "body" not in first and "raw_excerpt" not in first
        assert connection.execute("SELECT status FROM recruitment_title_leads").fetchone()[0] == "unverified"


def test_redirect_alias_cache_and_failed_refresh_keep_one_article(title_repo):
    short = "https://mp.weixin.qq.com/s/redirect-short"
    final = "https://mp.weixin.qq.com/s?__biz=PublicAccount&mid=123&idx=1&sn=abc"
    parser = Parser({short: metadata(final)})
    service = WechatTitleService(title_repo, parser=parser)
    original = asyncio.run(service.import_article(short))
    assert original["normalized_url"] == final
    assert asyncio.run(service.import_article(short + "?scene=2"))["cached"] is True
    assert asyncio.run(service.import_article(final))["cached"] is True
    assert len(parser.calls) == 1
    parser.items[short] = metadata(short, title=None, fetch_status="timeout", error="公开页面读取超时")
    failed = asyncio.run(service.import_article(short, force_refresh=True))
    assert failed["fetch_status"] == "timeout"
    assert failed["id"] == original["id"]
    assert failed["title"] == original["title"]
    assert failed["normalized_url"] == original["normalized_url"]
    assert failed["lead_id"] == original["lead_id"]
    assert count(title_repo, "source_articles") == count(title_repo, "recruitment_title_leads") == 1


def test_blocked_url_then_successful_redirect_does_not_leave_ghost_failure(title_repo):
    short = "https://mp.weixin.qq.com/s/blocked-before-redirect"
    final = "https://mp.weixin.qq.com/s?__biz=PublicAccount&mid=456&idx=1&sn=def"
    parser = Parser({short: metadata(short, title=None, fetch_status="blocked", error="需要访问验证")})
    service = WechatTitleService(title_repo, parser=parser)
    failed = asyncio.run(service.import_article(short))
    parser.items[short] = metadata(final)
    recovered = asyncio.run(service.import_article(short))
    assert failed["fetch_status"] == "blocked" and recovered["fetch_status"] == "success"
    assert recovered["normalized_url"] == final
    assert title_repo.get_by_url(short)["id"] == title_repo.get_by_url(final)["id"]
    assert count(title_repo, "source_articles") == count(title_repo, "recruitment_title_leads") == 1
    assert all(item["fetch_status"] == "success" for item in title_repo.list_articles()["items"])


def test_batch_concurrency_is_bounded_and_normalized_duplicates_share_fetch(title_repo):
    active = peak = 0
    calls = []

    async def parser(url, expected_source_name=None):
        nonlocal active, peak
        calls.append(url)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return metadata(url)
        finally:
            active -= 1

    service = WechatTitleService(title_repo, parser=parser)
    urls = [f"https://mp.weixin.qq.com/s/concurrent-{index}" for index in range(8)]
    result = asyncio.run(service.import_batch([*urls, urls[0] + "?scene=2"], force_refresh=True))
    assert 1 < peak <= 5
    assert len(calls) == 8
    assert (result["new"], result["duplicate"], result["failed"]) == (8, 1, 0)
    assert count(title_repo, "source_articles") == count(title_repo, "recruitment_title_leads") == 8


def test_partial_batch_isolated_failures_and_replay_do_not_duplicate_leads(title_repo):
    good, blocked, broken = (f"https://mp.weixin.qq.com/s/{name}" for name in ("good", "blocked", "broken"))
    parser = Parser({
        blocked: metadata(blocked, title=None, fetch_status="blocked", error="需要访问验证"),
        broken: RuntimeError("parser fixture failure"),
    })
    service = WechatTitleService(title_repo, parser=parser)
    result = asyncio.run(service.import_batch([good, blocked, "https://127.0.0.1/private", broken, good]))
    assert (result["total"], result["success"], result["new"], result["duplicate"], result["failed"]) == (5, 2, 1, 1, 3)
    assert result["ai_calls"] == result["model_tokens_used"] == 0
    assert count(title_repo, "source_articles") == 2
    assert count(title_repo, "recruitment_title_leads") == 1
    assert len(parser.calls) == 3
    errors = [item for item in title_repo.list_articles()["items"] if item["fetch_status"] == "blocked"]
    assert len(errors) == 1 and errors[0]["related_title_count"] == 0
    assert errors[0]["lead_id"] is None and errors[0]["title"] is None


def test_failed_refresh_preserves_page_metadata_dates_and_first_seen(title_repo):
    url = "https://mp.weixin.qq.com/s/known-metadata"
    published = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
    parser = Parser({url: metadata(url, published_at=published)})
    service = WechatTitleService(title_repo, parser=parser)
    original = asyncio.run(service.import_article(url, expected_source_name="国央校招"))
    assert original["source_name"] == "国聘" and original["source_name_detection"] == "page"
    parser.items[url] = metadata(
        url, title=None, published_at=None, source_name="国央校招",
        source_name_detection="configured", fetch_status="timeout", error="页面读取超时",
    )
    failed = asyncio.run(service.import_article(url, expected_source_name="国央校招", force_refresh=True))
    for key in ("title", "source_name", "source_name_detection", "published_at", "discovered_at", "lead_id"):
        assert failed[key] == original[key], key
    assert failed["fetch_status"] == "timeout"
    assert not failed["lead_created"]
    parser.items[url] = metadata(url, published_at=None, source_name=None, source_name_detection="unknown")
    recovered = asyncio.run(service.import_article(url, force_refresh=True))
    assert recovered["fetch_status"] == "success"
    assert recovered["published_at"] == original["published_at"]
    assert recovered["discovered_at"] == original["discovered_at"]


def test_title_fingerprints_are_hints_not_destructive_url_merges(title_repo):
    first_url, second_url = (f"https://mp.weixin.qq.com/s/{name}" for name in ("syndicated-one", "syndicated-two"))
    service = WechatTitleService(title_repo, parser=Parser())
    asyncio.run(service.import_batch([first_url, second_url]))
    listing = title_repo.list_articles()
    assert listing["total"] == 2
    assert {item["normalized_url"] for item in listing["items"]} == {first_url, second_url}
    assert len({item["metadata_fingerprint"] for item in listing["items"]}) == 1
    assert all(item["related_title_count"] == 1 for item in listing["items"])
    assert count(title_repo, "recruitment_title_leads") == 2


def test_filters_and_unknown_publication_dates_do_not_guess_dates(title_repo):
    urls = [f"https://mp.weixin.qq.com/s/filter-{index}" for index in range(3)]
    parser = Parser({
        urls[0]: metadata(urls[0], published_at=datetime(2026, 9, 10, tzinfo=timezone.utc)),
        urls[1]: metadata(urls[1], title="银行招聘公告", source_name="银行招聘网"),
        urls[2]: metadata(urls[2], title="金秋好风景", published_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
    })
    service = WechatTitleService(title_repo, parser=parser)
    asyncio.run(service.import_batch(urls))
    assert title_repo.list_articles()["total"] == 3
    assert title_repo.list_articles(relevance_status="possible")["items"][0]["published_at"] is None
    assert title_repo.list_articles(source_name="银行招聘网")["total"] == 1
    assert title_repo.list_articles(from_date="2026-09-05", to_date="2026-09-15")["total"] == 1
    assert title_repo.list_articles(to_date="2026-09-05")["total"] == 1
    assert title_repo.list_articles(page=2, page_size=2)["total"] == 3
    assert len(title_repo.list_articles(page=2, page_size=2)["items"]) == 1
    assert count(title_repo, "recruitment_title_leads") == 2


def test_no_provider_monitor_is_honest_and_does_not_repoll_old_seeds(title_repo):
    parser = Parser()
    result = asyncio.run(run_wechat_monitor(WechatTitleService(title_repo, parser=parser)))
    assert result["status"] == "provider_pending"
    assert len(result["accounts"]) == 5 and parser.calls == []
    assert result["ai_calls"] == result["model_tokens_used"] == 0
    assert count(title_repo, "source_articles") == 0


def test_router_authentication_consent_admin_policy_and_input_boundaries(title_repo):
    parser = Parser()
    service = WechatTitleService(title_repo, parser=parser)

    def current_user(authorization: str | None = Header(default=None)):
        if authorization not in {"Bearer reader", "Bearer consented"}:
            raise HTTPException(401, "Authentication required")
        return {"id": 1}

    def consented_user(authorization: str | None = Header(default=None)):
        current_user(authorization)
        if authorization != "Bearer consented":
            raise HTTPException(403, "Privacy consent required")
        return {"id": 1}

    def admin_auth(x_admin_token: str | None = Header(default=None)):
        if x_admin_token != "fixture-admin":
            raise HTTPException(401, "Administrator authorization required")

    app = FastAPI()
    app.include_router(create_wechat_router(
        service, current_user=current_user, consented_user=consented_user, admin_auth=admin_auth,
    ))
    url = "https://mp.weixin.qq.com/s/router-fixture"
    with TestClient(app) as client:
        for path in ("", "/articles", "/articles/review"):
            assert client.get("/api/sources/wechat" + path).status_code == 401
        assert client.post("/api/sources/wechat/article", json={"url": url}).status_code == 401
        assert client.post("/api/sources/wechat/article", headers={"Authorization": "Bearer reader"}, json={"url": url}).status_code == 403
        headers = {"Authorization": "Bearer consented"}
        assert client.post("/api/sources/wechat", headers=headers, json={"source_name": "New publisher"}).status_code == 401
        assert client.post("/api/sources/wechat", headers={"X-Admin-Token": "fixture-admin"}, json={"source_name": "New publisher"}).status_code == 200
        result = client.post("/api/sources/wechat/article", headers=headers, json={"url": url})
        assert result.status_code == 200 and result.json()["verification_status"] == "unverified"
        assert client.get("/api/sources/wechat/articles", headers=headers).json()["total"] == 1
        assert client.post("/api/sources/wechat/articles/import", headers=headers, json={"urls": [url] * 51}).status_code == 422
        assert client.post("/api/sources/wechat/article", headers=headers, json={"url": url, "body": "do not store"}).status_code == 422
        assert client.get("/api/sources/wechat/articles?from_date=2026-09-10&to_date=2026-09-01", headers=headers).status_code == 422
        assert client.get("/api/sources/wechat/articles?page_size=101", headers=headers).status_code == 422
        assert client.get("/api/sources/wechat/articles?relevance_status=verified", headers=headers).status_code == 422
        assert client.post("/api/sources/wechat/monitor", headers=headers).json()["status"] == "provider_pending"
    assert len(parser.calls) == 1


@pytest.mark.parametrize('size', [5, 20, 50])
def test_required_batch_sizes_commit_independently(title_repo, size):
    urls = [f'https://mp.weixin.qq.com/s/batch-{index}' for index in range(size)]
    parser = Parser({urls[-1]: metadata(urls[-1], title=None, fetch_status='http_403')})
    result = asyncio.run(WechatTitleService(title_repo, parser=parser).import_batch(urls))
    assert (result['total'], result['success'], result['new'], result['failed']) == (size, size - 1, size - 1, 1)
    assert count(title_repo, 'recruitment_title_leads') == size - 1


def test_monitor_processes_all_discoveries_and_reports_partial(title_repo):
    for source in title_repo.list_sources():
        title_repo.add_source(source['source_name'], source['seed_url'], source['source_name'] == '国聘')
    urls = [f'https://mp.weixin.qq.com/s/monitor-{index}' for index in range(51)]
    class Provider:
        async def discover(self, source):
            assert source['source_name'] == '国聘'
            return [{'url': url} for url in urls]
    parser = Parser({urls[-1]: metadata(urls[-1], title=None, fetch_status='http_403')})
    result = asyncio.run(run_wechat_monitor(WechatTitleService(title_repo, parser=parser), Provider()))
    assert result['status'] == 'partial'
    assert result['accounts'][0]['total'] == 51
    assert result['accounts'][0]['success'] == 50
    assert len(parser.calls) == 51


def test_one_click_watchlist_import_preserves_each_configured_account(title_repo):
    parser = Parser()

    async def configured_parser(url, expected_source_name=None):
        parser.calls.append((url, expected_source_name))
        return metadata(
            url, source_name=expected_source_name,
            source_name_detection="configured",
            title=f"{expected_source_name} 2027届校园招聘",
        )

    service = WechatTitleService(title_repo, parser=configured_parser)
    result = asyncio.run(service.import_watchlist_seeds())
    assert (result["total"], result["success"], result["new"], result["failed"]) == (5, 5, 5, 0)
    assert result["scope"] == "configured_watchlist_seeds"
    assert {name for _, name in parser.calls} == {name for name, _ in WATCHLIST}
    assert {item["source_name"] for item in result["items"]} == {name for name, _ in WATCHLIST}
    assert count(title_repo, "source_articles") == count(title_repo, "recruitment_title_leads") == 5
    replay = asyncio.run(service.import_watchlist_seeds())
    assert (replay["new"], replay["duplicate"], len(parser.calls)) == (0, 5, 5)


def test_duplicate_discovery_and_cache_do_not_duplicate_pending_leads(title_repo):
    calls = []
    class Provider:
        name = 'sogou_wechat'
        async def discover(self, source):
            calls.append(source['source_name'])
            return [DiscoveredArticle(
                url='https://weixin.sogou.com/link?url=one',
                discovery_url='https://weixin.sogou.com/link?url=one',
                title='国聘2027届校园招聘启动', source_name=source['source_name'],
                expected_source_name=source['source_name'], provider=self.name,
            )]
    for source in title_repo.list_sources():
        title_repo.add_source(source['source_name'], source['seed_url'], source['source_name'] == '国聘')
    service = WechatTitleService(title_repo, parser=Parser())
    first = asyncio.run(service.discover_now(Provider(), respect_cooldown=False))
    second = asyncio.run(service.discover_now(Provider(), respect_cooldown=False))
    assert first['counts']['new'] == 1
    assert second['counts']['duplicate'] == 1 and second['counts']['cached_accounts'] == 1
    assert calls == ['国聘']
    assert count(title_repo, 'source_articles') == count(title_repo, 'recruitment_title_leads') == 1


def test_provider_failure_does_not_break_manual_import(title_repo):
    class BlockedProvider:
        name = 'sogou_wechat'
        async def discover(self, source):
            raise DiscoveryProviderUnavailable('公开搜索访问受限（HTTP 403）。')
    service = WechatTitleService(title_repo, parser=Parser())
    failed = asyncio.run(service.discover_now(BlockedProvider(), respect_cooldown=False))
    assert failed['status'] == 'unavailable' and failed['counts']['failed'] == 1
    manual = asyncio.run(service.import_article('https://mp.weixin.qq.com/s/manual-after-failure'))
    assert manual['fetch_status'] == 'success' and manual['is_new']


def test_discovery_cooldown_blocks_repeated_manual_trigger(title_repo):
    class EmptyProvider:
        name = 'sogou_wechat'
        async def discover(self, source):
            return []
    service = WechatTitleService(title_repo, parser=Parser())
    assert asyncio.run(service.discover_now(EmptyProvider(), respect_cooldown=False))['status'] == 'available'
    with pytest.raises(DiscoveryCooldown) as error:
        asyncio.run(service.discover_now(EmptyProvider()))
    assert error.value.retry_after > 0


def test_manual_and_scheduled_discovery_cannot_overlap(title_repo):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider:
        name = 'sogou_wechat'

        async def discover(self, source):
            started.set()
            await release.wait()
            return []

    async def scenario():
        service = WechatTitleService(title_repo, parser=Parser())
        running = asyncio.create_task(
            service.discover_now(SlowProvider(), respect_cooldown=False)
        )
        await started.wait()
        duplicate = await service.discover_now(SlowProvider(), respect_cooldown=False)
        release.set()
        completed = await running
        return duplicate, completed

    duplicate, completed = asyncio.run(scenario())
    assert duplicate['status'] == 'already_running'
    assert completed['status'] == 'available'
