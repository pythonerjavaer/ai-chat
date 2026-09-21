"""All connector network checks are mocked; CI never contacts WeChat."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from backend.future_radar.wechat.classifier import classify_recruitment_title
from backend.future_radar.wechat.connector import WechatArticleConnector
from backend.future_radar.wechat.discovery.manual import ManualDiscoveryProvider
from backend.future_radar.wechat.discovery.search import SearchDiscoveryProvider, build_discovery_queries
from backend.future_radar.wechat.fetcher import PublicPageResult, fetch_public_page
from backend.future_radar.wechat.normalizer import (
    UnsafeWechatURLError, metadata_fingerprint, normalize_wechat_url, validate_wechat_article_url,
)
from backend.future_radar.wechat.parser import parse_wechat_article_metadata


FIXTURES = Path(__file__).parent / "fixtures" / "wechat"
URL = "https://mp.weixin.qq.com/s/public-seed"


def run(awaitable):
    return asyncio.run(awaitable)


def parse_fixture(name: str, expected: str | None = None):
    async def fetch(url):
        return PublicPageResult(url, url, "success", metadata_html=(FIXTURES / name).read_text())
    return run(parse_wechat_article_metadata(URL, expected, fetcher=fetch))


def public_dns(_host):
    return ["8.8.8.8", "2001:4860:4860::8888"]


def test_valid_wechat_url():
    assert validate_wechat_article_url(URL) == URL


@pytest.mark.parametrize("url", [
    "https://example.com/s/public", "http://mp.weixin.qq.com/s/public",
    "https://mp.weixin.qq.com.evil.test/s/public", "https://mp.weixin.qq.com:8443/s/public",
    "https://evil@mp.weixin.qq.com/s/public", "https://mp.weixin.qq.com/mp/profile_ext",
    "file:///tmp/a", "ftp://mp.weixin.qq.com/s/public", "https://mp.weixin.qq.com/s/public?access_token=secret",
])
def test_reject_non_wechat_url(url):
    with pytest.raises(UnsafeWechatURLError):
        validate_wechat_article_url(url)


@pytest.mark.parametrize("url", ["https://localhost/s/a", "https://127.0.0.1/s/a", "https://192.168.1.1/s/a"])
def test_reject_localhost(url):
    with pytest.raises(UnsafeWechatURLError):
        validate_wechat_article_url(url)


def test_title_from_og_title():
    result = parse_fixture("article_og_title.html")
    assert result.title == "中国银行2027年全球校园招聘正式启动"
    assert result.source_name == "国聘"
    assert result.source_name_detection == "page"
    assert result.fetch_status == "success"
    assert result.published_at.isoformat() == "2026-09-21T10:00:00+08:00"


def test_title_from_dom():
    result = parse_fixture("article_dom_title.html")
    assert result.title == "2027届中国移动校园招聘全面开启"
    assert result.source_name == "国央校招"
    assert result.published_at is None


def test_title_from_html_title():
    async def fetch(url):
        return PublicPageResult(url, url, "success", metadata_html="<html><title>某企业校园招聘启动</title></html>")
    assert run(parse_wechat_article_metadata(URL, fetcher=fetch)).title == "某企业校园招聘启动"


def test_title_parse_failure():
    result = parse_fixture("article_missing_title.html")
    assert result.fetch_status == "parse_failed"
    assert result.title is None
    assert result.source_name is None


def test_source_name_parser():
    result = parse_fixture("article_recruitment.html")
    assert result.source_name == "国资小新"
    assert result.source_name_detection == "page"
    assert result.published_at is not None


def test_source_name_config_fallback():
    async def fetch(url):
        return PublicPageResult(url, url, "success", metadata_html='<meta property="og:title" content="中国银行招聘">')
    result = run(parse_wechat_article_metadata(URL, "银行招聘网", fetcher=fetch))
    assert result.source_name == "银行招聘网"
    assert result.source_name_detection == "configured"
    unknown = run(parse_wechat_article_metadata(URL, fetcher=fetch))
    assert unknown.source_name is None
    assert unknown.source_name_detection == "unknown"


def test_page_source_is_not_overwritten_by_configuration():
    result = parse_fixture("article_og_title.html", "另一个来源")
    assert result.source_name == "国聘"
    assert result.source_name_detection == "page"


def test_unreliable_dates_remain_unknown():
    async def fetch(url):
        return PublicPageResult(url, url, "success", metadata_html='<title>银行校园招聘</title><meta property="article:published_time" content="昨天">2027届 2026-09-21')
    assert run(parse_wechat_article_metadata(URL, fetcher=fetch)).published_at is None


@pytest.mark.parametrize("title", [
    "中国银行2027年全球校园招聘正式启动", "2027届中国移动校园招聘全面开启",
    "国家能源集团2027年度高校毕业生招聘公告", "工商银行2027年度校园招聘公告",
])
def test_relevant_recruitment_title(title):
    result = classify_recruitment_title(title)
    assert result.relevance_status == "relevant"
    assert result.relevance_score >= 85


@pytest.mark.parametrize("title", ["本周央国企招聘信息汇总", "这些银行正在招聘", "最新国企招聘岗位来了"])
def test_possible_recruitment_title(title):
    assert classify_recruitment_title(title).relevance_status == "possible"


@pytest.mark.parametrize("title", ["央企改革最新进展", "中国银行发布半年报", "国资委召开专题会议", "银行业最新政策解读"])
def test_irrelevant_title(title):
    assert classify_recruitment_title(title).relevance_status == "irrelevant"


def test_negative_social_recruitment_weight():
    regular = classify_recruitment_title("2027中国银行校园招聘")
    social = classify_recruitment_title("2027中国银行校园招聘与社会招聘")
    assert social.relevance_score < regular.relevance_score
    assert "社会招聘" in social.matched_keywords
    assert classify_recruitment_title("银行社会招聘岗位").relevance_status == "irrelevant"


def test_combination_bonus_2027_campus():
    cohort = classify_recruitment_title("某企业2027届校园招聘")
    plain = classify_recruitment_title("某企业校园招聘")
    assert cohort.relevance_score > plain.relevance_score
    assert "2027届" in cohort.matched_keywords


def test_normalized_url_dedup():
    assert normalize_wechat_url(URL + "?scene=1&from=timeline#wechat_redirect") == URL
    first = "https://mp.weixin.qq.com/s?__biz=AbC%3D%3D&mid=123&idx=1&sn=abc&scene=1"
    second = "https://mp.weixin.qq.com/s?idx=1&mid=123&sn=abc&__biz=AbC%3D%3D&chksm=other"
    assert normalize_wechat_url(first) == normalize_wechat_url(second)
    assert normalize_wechat_url(second.replace("idx=1", "idx=2")) != normalize_wechat_url(first)


def test_conflicting_identity_query_is_rejected():
    with pytest.raises(UnsafeWechatURLError):
        normalize_wechat_url("https://mp.weixin.qq.com/s?__biz=AbC&mid=1&idx=1&idx=2")


def test_metadata_fingerprint():
    assert metadata_fingerprint(" 国聘 ", "2027 校园招聘") == metadata_fingerprint("国聘", "２０２７校园招聘")
    assert metadata_fingerprint("国聘", "校园招聘") != metadata_fingerprint("国央校招", "校园招聘")


def test_fetch_timeout():
    calls = []
    async def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("not logged", request=request)
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(handler), resolver=public_dns))
    assert result.fetch_status == "timeout"
    assert len(calls) == 2


def test_http_403():
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(403)
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(handler), resolver=public_dns))
    assert result.fetch_status == "http_403"
    assert len(calls) == 1


@pytest.mark.parametrize("status, expected", [(404, "http_404"), (429, "http_error"), (500, "http_error")])
def test_other_http_statuses(status, expected):
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(lambda request: httpx.Response(status)), resolver=public_dns))
    assert result.fetch_status == expected


def test_redirect_ssrf_is_rejected_before_request():
    visited = []
    async def handler(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(handler), resolver=public_dns))
    assert result.fetch_status == "unsafe_url"
    assert visited == [URL]


def test_private_dns_is_rejected_before_request():
    def handler(request):
        pytest.fail("must reject DNS before fetching")
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(handler), resolver=lambda _: ["127.0.0.1"]))
    assert result.fetch_status == "unsafe_url"


def test_public_redirect_rechecks_dns_and_does_not_send_cookies():
    resolved, requests = [], []
    def resolver(host):
        resolved.append(host)
        return ["8.8.8.8"]
    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "/s/canonical-article", "set-cookie": "session=do-not-send"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>校园招聘</title>")
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(handler), resolver=resolver))
    assert result.fetch_status == "success"
    assert len(resolved) == 2
    assert "cookie" not in requests[1].headers
    assert result.final_url == "https://mp.weixin.qq.com/s/canonical-article"


@pytest.mark.parametrize("body_tag", [
    b'<div id="js_content">', b'<div class=rich_media_content>',
    b'<section class=rich_media_content>', b'<div id=js_content>',
])
def test_body_stream_is_closed_without_downloading_remainder(body_tag):
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'<html><meta property="og:title" content="Recruitment">' + body_tag + b" " * 1_024
            pytest.fail("must not consume the article body stream")
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)
    result = run(fetch_public_page(URL, transport=httpx.MockTransport(handler), resolver=public_dns))
    assert result.fetch_status == "success"
    assert "js_content" not in result.metadata_html
    assert "rich_media_content" not in result.metadata_html
    assert stream.closed


def test_captcha_does_not_become_a_title_or_trigger_bypass():
    async def fetch(url):
        return PublicPageResult(url, url, "success", metadata_html="<title>环境异常</title><p>完成验证后即可继续访问</p>")
    result = run(parse_wechat_article_metadata(URL, fetcher=fetch))
    assert result.fetch_status == "blocked"
    assert result.title is None


def test_captcha_script_reference_is_not_an_access_challenge():
    async def fetch(url):
        return PublicPageResult(url, url, "success", metadata_html='<meta property="og:title" content="银行校园招聘"><script src="/captcha.js"></script>')
    result = run(parse_wechat_article_metadata(URL, fetcher=fetch))
    assert result.fetch_status == "success"


def test_manual_discovery_deduplicates_without_network():
    provider = ManualDiscoveryProvider([URL, URL + "?scene=1"])
    result = run(provider.discover({"source_name": "国聘"}))
    assert len(result) == 1
    assert result[0].expected_source_name == "国聘"


def test_search_provider_is_abstract_and_queries_do_not_fetch():
    with pytest.raises(TypeError):
        SearchDiscoveryProvider()
    queries = build_discovery_queries({"source_name": "国聘"})
    assert queries == ["国聘", "国聘 2027", "国聘 校园招聘", "国聘 秋招"]


def test_connector_keeps_discovery_separate_from_fetch():
    calls = []
    async def fetch(url):
        calls.append(url)
        return PublicPageResult(url, url, "http_404", error="not found")
    connector = WechatArticleConnector(ManualDiscoveryProvider([URL]), fetcher=fetch)
    assert len(run(connector.discover({"source_name": "国聘"}))) == 1
    assert calls == []
    assert run(connector.parse(URL)).fetch_status == "http_404"
    assert calls == [URL]
