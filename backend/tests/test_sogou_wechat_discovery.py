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
    exact_source_match, parse_sogou_results,
)


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


def test_similar_account_rejected():
    assert not exact_source_match("国聘", "XX国聘信息网")
    assert parse_sogou_results(result_html("XX国聘信息网"), "国聘") == []


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
