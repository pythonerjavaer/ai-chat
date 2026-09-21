"""A confirmed public redirect must consolidate metadata identity and leads."""

import asyncio
from contextlib import closing
import os
import sqlite3

import pytest

os.environ.setdefault("JWT_SECRET", "wechat-title-fixture-secret-at-least-32-characters")

from backend.future_radar.schema import migrate
from backend.future_radar.wechat.models import WechatArticleMetadata
from backend.future_radar.wechat.repository import WechatTitleRepository
from backend.future_radar.wechat.service import WechatTitleService


SHORT = "https://mp.weixin.qq.com/s/redirect-regression"
LONG = "https://mp.weixin.qq.com/s?__biz=PublicAccount&mid=123&idx=1&sn=abc"


@pytest.fixture
def redirect_service(tmp_path):
    path = tmp_path / "redirect.sqlite3"

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
    state = {"redirect": False, "fail": False, "calls": 0}

    async def parser(url, expected_source_name=None):
        state["calls"] += 1
        final_url = LONG if state["redirect"] and url == SHORT else url
        if state["fail"]:
            return WechatArticleMetadata(
                url=url, normalized_url=url, fetch_status="timeout", error="公开文章读取超时。",
            )
        return WechatArticleMetadata(
            url=final_url, normalized_url=final_url, title="中国银行2027届校园招聘正式启动",
            source_name="国聘", source_name_detection="page", fetch_status="success",
        )

    return WechatTitleService(repo, parser=parser), state


def assert_one_document_and_lead(repo):
    assert repo.list_articles()["total"] == 1
    with closing(repo._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recruitment_title_leads").fetchone()[0] == 1
        assert connection.execute("SELECT source_url FROM recruitment_title_leads").fetchone()[0] == LONG
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_successful_short_article_gains_canonical_url_without_second_lead(redirect_service):
    service, state = redirect_service
    original = asyncio.run(service.import_article(SHORT))
    state["redirect"] = True
    refreshed = asyncio.run(service.import_article(SHORT, force_refresh=True))

    assert refreshed["id"] == original["id"]
    assert refreshed["lead_id"] == original["lead_id"]
    assert refreshed["normalized_url"] == LONG
    assert refreshed["is_new"] is False
    assert refreshed["lead_created"] is False
    assert refreshed["discovered_at"] == original["discovered_at"]
    for url in (SHORT, LONG):
        cached = asyncio.run(service.import_article(url))
        assert cached["cached"] and cached["id"] == original["id"]
    assert state["calls"] == 2
    assert_one_document_and_lead(service.repository)


def test_independently_imported_aliases_merge_once_redirect_is_observed(redirect_service):
    service, state = redirect_service
    short_record = asyncio.run(service.import_article(SHORT))
    canonical_record = asyncio.run(service.import_article(LONG))
    assert short_record["id"] != canonical_record["id"]
    state["redirect"] = True
    refreshed = asyncio.run(service.import_article(SHORT, force_refresh=True))

    assert refreshed["id"] == canonical_record["id"]
    assert refreshed["lead_id"] == canonical_record["lead_id"]
    assert not refreshed["is_new"] and not refreshed["lead_created"]
    assert service.repository.get_by_url(SHORT)["id"] == canonical_record["id"]
    assert service.repository.get_by_url(LONG)["id"] == canonical_record["id"]
    assert_one_document_and_lead(service.repository)


def test_failed_force_refresh_of_original_alias_keeps_known_identity(redirect_service):
    service, state = redirect_service
    original = asyncio.run(service.import_article(SHORT))
    state["redirect"] = True
    redirected = asyncio.run(service.import_article(SHORT, force_refresh=True))
    state["fail"] = True
    failed = asyncio.run(service.import_article(SHORT, force_refresh=True))

    assert failed["fetch_status"] == "timeout"
    assert failed["id"] == redirected["id"] == original["id"]
    assert failed["normalized_url"] == LONG
    assert failed["title"] == original["title"]
    assert failed["source_name"] == "国聘" and failed["source_name_detection"] == "page"
    assert failed["lead_id"] == original["lead_id"]
    assert not failed["is_new"] and not failed["lead_created"]
    assert_one_document_and_lead(service.repository)
