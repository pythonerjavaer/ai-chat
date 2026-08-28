from types import SimpleNamespace

import pytest

from backend.future_radar.public_discovery import (
    BANK_RECRUITMENT_URLS,
    SASAC_RECRUITMENT_URLS,
    PublicDiscoveryUnavailable,
    discover_bank_recruitment_articles,
    discover_sasac_recruitment_articles,
)
from backend.recruitment_watch import WatchFetchError


def _page(url: str, html: str, fingerprint: str = "public-page-v1"):
    return SimpleNamespace(
        final_url=url,
        raw_text=html,
        text="visible public listing",
        fingerprint=fingerprint,
    )


def test_sasac_discovery_uses_public_listing_and_sanitizes_metadata():
    secret_marker = "sk" + "-proj-NOT_A_REAL_TEST_SECRET_123456"
    html = f"""
    <html><body><ul>
      <li><span>2026-08-27</span>
        <a href="/n2588035/n2588325/n2588350/c123456/content.html?utm_source=feed">
          某中央企业2027届校园招聘公告
        </a>
        联系人 public@example.com，13800138000，{secret_marker}
      </li>
      <li><span>2026-08-26</span><a href="/policy/notice.html">国资监管政策</a></li>
      <li><a href="https://evil.example/jobs">某央企校园招聘</a></li>
      <li><a href="/jobs/private.html?token=credential">某央企2027届校招</a></li>
    </ul></body></html>
    """

    batch = discover_sasac_recruitment_articles(
        fetcher=lambda *_args, **_kwargs: _page(SASAC_RECRUITMENT_URLS[0], html)
    )

    assert batch.source_id == "public-sasac-recruitment"
    assert len(batch.articles) == 1
    article = batch.articles[0]
    assert article.publisher == "国务院国资委"
    assert article.title == "某中央企业2027届校园招聘公告"
    assert article.url == (
        "https://www.sasac.gov.cn/n2588035/n2588325/n2588350/c123456/content.html"
    )
    assert article.publish_time == "2026-08-27"
    assert article.recruitment_year == 2027
    assert article.classification == "campus_recruitment_signal"
    assert "public@example.com" not in article.raw_excerpt
    assert "13800138000" not in article.raw_excerpt
    assert "sk-proj-" not in article.raw_excerpt
    assert "[redacted-email]" in article.raw_excerpt
    assert set(article.to_dict()) == {
        "publisher", "title", "url", "publish_time", "raw_excerpt",
        "is_recruitment", "recruitment_year", "classification",
    }


def test_sasac_discovery_falls_back_to_public_wap_listing():
    calls: list[str] = []
    html = """
    <ul><li><a href="/n1/c2/content.html">某集团2027年校园招聘正式启动</a>
    <time>2026年08月28日</time></li></ul>
    """

    def fetcher(url, *_args, **_kwargs):
        calls.append(url)
        if url == SASAC_RECRUITMENT_URLS[0]:
            raise WatchFetchError("primary unavailable")
        return _page(SASAC_RECRUITMENT_URLS[1], html, "wap-v1")

    batch = discover_sasac_recruitment_articles(fetcher=fetcher)

    assert calls == list(SASAC_RECRUITMENT_URLS)
    assert batch.source_url == SASAC_RECRUITMENT_URLS[1]
    assert len(batch.articles) == 1
    assert batch.articles[0].publish_time == "2026-08-28"


def test_bank_recruitment_is_discovery_only_and_filters_commercial_noise():
    html = """
    <main>
      <div><a href="/news/2027-campus.html?spm=home">
        中国银行2027届校园招聘公告
      </a><span>2026/08/28</span></div>
      <div><a href="/news/social.html">某银行社会招聘启事</a>
        <span>2026-08-27</span></div>
      <div><a href="/news/result.html">某银行2026年校园招聘拟录用人员公示</a></div>
      <div><a href="/courses/exam.html">银行招聘考试培训课程</a></div>
      <div><a href="javascript:void(0)">招商银行2027校园招聘</a></div>
      <div><a href="/about/video.html">视听中心</a>
        <span>附近区域写着校园招聘</span></div>
    </main>
    """
    batch = discover_bank_recruitment_articles(
        fetcher=lambda *_args, **_kwargs: _page(BANK_RECRUITMENT_URLS[0], html)
    )

    assert [article.classification for article in batch.articles] == [
        "campus_recruitment_signal",
        "social_recruitment_signal",
        "recruitment_result_signal",
    ]
    assert all(article.is_recruitment for article in batch.articles)
    assert all(not hasattr(article, "verification_status") for article in batch.articles)
    assert all("verified" not in article.to_dict() for article in batch.articles)
    # The nearby publication date is not guessed to be the campaign cohort.
    assert batch.articles[1].recruitment_year is None
    assert batch.articles[2].recruitment_year == 2026
    assert batch.articles[0].url == "https://www.yhks.cn/news/2027-campus.html"

    radar_article = batch.articles[0].to_radar_article()
    assert radar_article["article_title"] == "中国银行2027届校园招聘公告"
    assert radar_article["article_url"] == "https://www.yhks.cn/news/2027-campus.html"
    assert "title" not in radar_article
    assert "url" not in radar_article


def test_public_discovery_does_not_fabricate_success_when_all_endpoints_fail():
    def unavailable(*_args, **_kwargs):
        raise WatchFetchError("offline")

    with pytest.raises(PublicDiscoveryUnavailable, match="没有伪造成功结果"):
        discover_sasac_recruitment_articles(fetcher=unavailable)


def test_public_discovery_enforces_a_bounded_article_batch():
    with pytest.raises(ValueError, match="between 1 and 100"):
        discover_bank_recruitment_articles(max_articles=101, fetcher=lambda: None)
