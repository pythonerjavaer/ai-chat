"""Mocked public-search discovery tests; no live Sogou or WeChat traffic."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.future_radar.wechat.discovery.base import DiscoveryProviderUnavailable
from backend.future_radar.wechat.discovery.manual import ManualDiscoveryProvider
from backend.future_radar.wechat.discovery.sogou import (
    MAX_SEARCH_RESPONSE_BYTES, SogouWechatDiscoveryProvider,
    exact_source_match, parse_sogou_candidates, parse_sogou_results,
)
from backend.future_radar.wechat.discovery.search import build_discovery_queries


def result_html(source: str = "国聘", *, epoch: int = 1789948800, title: str = "国聘2027届校园招聘启动") -> str:
    return f'''<html><ul class="news-list"><li><div class="txt-box">
      <h3><a href="/link?url=public-result"><em>{title}</em></a></h3>
      <div class="s-p"><a>{source}</a><span><script>document.write(timeConvert('{epoch}'))</script></span></div>
    </div></li></ul></html>'''


def parsed_item():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    return parse_sogou_results(result_html(), "国聘", now=now)[0]


def test_sogou_parse_title():
    assert parsed_item().title == "国聘2027届校园招聘启动"


def test_sogou_parse_source_name():
    assert parsed_item().source_name == "国聘"


def test_sogou_parse_publish_time():
    assert parsed_item().published_at == datetime.fromtimestamp(1789948800, timezone.utc)


def test_exact_account_match():
    assert exact_source_match(" 银行招聘网 ", "银行招聘网")


def test_multiple_recruitment_queries_keep_base_query():
    assert build_discovery_queries("国聘") == ["国聘", "国聘 2027", "国聘 校园招聘", "国聘 秋招"]


def test_similar_account_rejected():
    assert not exact_source_match("国聘", "XX国聘信息网")
    assert parse_sogou_results(result_html("XX国聘信息网"), "国聘") == []


def test_candidate_debug_keeps_source_mismatch_and_raw_fields():
    candidate = parse_sogou_candidates(result_html("XX国聘信息网"), "国聘")[0]
    assert candidate["raw_title"] == "国聘2027届校园招聘启动"
    assert candidate["raw_source_name"] == "XX国聘信息网"
    assert candidate["normalized_source_name"] == "xx国聘信息网"
    assert candidate["accepted"] is False
    assert candidate["rejection_reason"] == "source_name_mismatch"


def test_candidate_debug_marks_old_exact_article_out_of_range():
    candidate = parse_sogou_candidates(
        result_html(epoch=1_700_000_000), "国聘", now=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )[0]
    assert candidate["accepted"] is False
    assert candidate["rejection_reason"] == "date_out_of_range"


def test_result_markup_with_void_thumbnail_and_span_source_is_parsed():
    page = '''<ul class="news-list"><li><img src="thumb"><div><h3><a href="/link?url=x">招聘标题</a></h3>
      <div class="s-p"><span class="all-time-y2">国聘</span><script>document.write(timeConvert('1789948800'))</script></div>
      </div></li></ul>'''
    rows = parse_sogou_results(page, "国聘", now=datetime(2026, 9, 22, tzinfo=timezone.utc))
    assert len(rows) == 1 and rows[0].source_name == "国聘"


def test_result_without_direct_wechat_url_is_preserved():
    async def requester(url, **kwargs):
        if kwargs.get("params"):
            return SimpleNamespace(status_code=200, url=url, text=result_html())
        return SimpleNamespace(status_code=200, url=url, text="ordinary public redirect page")
    result = asyncio.run(SogouWechatDiscoveryProvider(requester).discover({"source_name": "国聘"}))
    assert len(result) == 1 and result[0].article_url is None
    assert result[0].title and result[0].discovery_url


def test_normal_redirect_resolution():
    async def requester(url, **kwargs):
        if kwargs.get("params"):
            return SimpleNamespace(status_code=200, url=url, text=result_html())
        return SimpleNamespace(status_code=200, url="https://mp.weixin.qq.com/s/public-final", text="")
    result = asyncio.run(SogouWechatDiscoveryProvider(requester).discover({"source_name": "国聘"}))
    assert result[0].article_url == "https://mp.weixin.qq.com/s/public-final"


def test_query_is_recorded_on_discovery_candidates():
    async def requester(url, **kwargs):
        if kwargs.get("params"):
            return SimpleNamespace(status_code=200, url=url, text=result_html())
        return SimpleNamespace(status_code=200, url=url, text="ordinary public redirect page")
    items, candidates = asyncio.run(SogouWechatDiscoveryProvider(requester).discover_with_debug(
        {"source_name": "国聘"}, query="国聘 校园招聘",
    ))
    assert items[0].found_by_queries == ["国聘 校园招聘"]
    assert candidates[0]["query"] == "国聘 校园招聘"


def assert_unavailable(status, text):
    async def requester(url, **kwargs):
        return SimpleNamespace(status_code=status, url=url, text=text)
    with pytest.raises(DiscoveryProviderUnavailable):
        asyncio.run(SogouWechatDiscoveryProvider(requester).discover({"source_name": "国聘"}))


def test_captcha_marks_provider_unavailable():
    assert_unavailable(200, "请输入验证码")


def test_403_marks_provider_unavailable():
    assert_unavailable(403, "denied")


def test_oversized_search_page_marks_provider_unavailable():
    assert_unavailable(200, '<ul class="news-list">' + 'x' * MAX_SEARCH_RESPONSE_BYTES)


def test_manual_provider_remains_available_after_sogou_failure():
    async def requester(url, **kwargs):
        return SimpleNamespace(status_code=429, url=url, text="limited")
    with pytest.raises(DiscoveryProviderUnavailable):
        asyncio.run(SogouWechatDiscoveryProvider(requester).discover({"source_name": "国聘"}))
    manual = asyncio.run(ManualDiscoveryProvider([
        "https://mp.weixin.qq.com/s/manual-still-works",
    ]).discover({"source_name": "国聘"}))
    assert manual[0].article_url == "https://mp.weixin.qq.com/s/manual-still-works"
