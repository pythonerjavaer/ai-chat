"""Deterministic contract for the official Zhong Ou Fund campus source."""

import os
from copy import deepcopy
from types import SimpleNamespace


os.environ.setdefault("OPENAI_API_KEY", "zofund-source-test-unused")
os.environ.setdefault("JWT_SECRET", "zofund-source-test-secret-32-chars")
os.environ.setdefault("RECRUITMENT_WEB_SEARCH_ENABLED", "false")

from backend import recruitment_search as search
from backend.future_radar import adapters
from backend.future_radar.normalization import clean_text
from backend.future_radar.seeds import VERIFIED_OFFICIAL_SOURCES
from backend.recruitment_directory import employer_directory_category
from backend.recruitment_watch import normalize_html_text


SOURCE_ID = "official-zofund-campus-2027"
CATEGORY = "securities_public_funds_asset_management"
EXPECTED_JOBS = {
    "621132304": ("27届校招-信用研究", "上海市"),
    "621076397": ("27届校招-权益研究", "上海市"),
    "621080967": ("27届校招-量化研究", "上海市"),
    "621076417": ("27届校招-多资产策略研究", "上海市"),
    "621087801": ("27届校招-AI投研方案研究", "上海市"),
    "621082595": ("27届校招-渠道销售（湖南）", "湖南省-长沙市"),
    "621074319": ("27届校招-财务管理", "上海市"),
    "621076462": ("27届校招-AI应用开发", "上海市"),
    "621076388": ("27届校招-系统开发", "上海市"),
    "620953573": ("27届校招-量化风控", "上海市"),
}


def _source():
    return deepcopy(next(item for item in VERIFIED_OFFICIAL_SOURCES if item["id"] == SOURCE_ID))


def test_zofund_directory_alias_and_official_host_are_explicit():
    assert employer_directory_category("中欧基金") == CATEGORY
    assert employer_directory_category("中欧基金管理有限公司") == CATEGORY
    assert search.OFFICIAL_RECRUITMENT_DOMAINS_BY_EMPLOYER["中欧基金"] == (
        "zofund.zhiye.com",
    )
    assert search._official_domain_confirmed(
        "中欧基金管理有限公司",
        "https://zofund.zhiye.com/campusxq?jobId=621132304",
        ("中欧基金",),
    )


def test_zofund_seed_has_ten_literal_jobs_and_no_invented_deadlines():
    source = _source()
    assert source["source_type"] == "official_html"
    assert source["url"] == "https://zofund.zhiye.com/campus"
    assert source["company"] == "中欧基金"
    config = source["adapter_config"]
    assert config["adapter"] == "official_html"
    assert config["ai_extract"] is False
    assert config["recruitment_year"] == 2027
    assert config["primary_category"] == CATEGORY

    jobs = config["configured_jobs"]
    assert len(jobs) == len(EXPECTED_JOBS) == 10
    assert len({job["application_url"] for job in jobs}) == 10
    assert all("opening_date" not in job and "closing_date" not in job for job in jobs)
    for job in jobs:
        job_id = job["external_id"].removeprefix("zofund-campus-")
        title, city = EXPECTED_JOBS[job_id]
        assert job["job_marker"] == title
        assert job["job_title"] == title
        assert job["city"] == city
        assert job["application_url"] == (
            f"https://zofund.zhiye.com/campusxq?jobId={job_id}"
        )


def test_zofund_official_html_adapter_emits_exact_clickable_jobs(monkeypatch):
    source = _source()
    titles = [title for title, _city in EXPECTED_JOBS.values()]
    page = SimpleNamespace(
        final_url=source["url"],
        fingerprint="zofund-campus-page-v1",
        keyword_hits=["校园招聘"],
        text=normalize_html_text("中欧基金 校园招聘 " + " ".join(titles)),
    )
    monkeypatch.setattr(adapters.DOMAIN_LIMITER, "wait", lambda *_args: None)
    monkeypatch.setattr(adapters, "fetch_watch_page", lambda *_args, **_kwargs: page)

    result = adapters.OfficialHtmlAdapter(
        repository=None,
        api_key="unused",
        ai_model="unused",
    ).scan(source)

    assert result.status == "healthy"
    assert len(result.jobs) == 10
    assert {job["title"] for job in result.jobs} == {clean_text(title) for title in titles}
    assert all(job["verification_status"] == "verified" for job in result.jobs)
    assert all(job["status"] == "open" for job in result.jobs)
    assert all(job["closing_date"] is None for job in result.jobs)
    assert {job["application_url"] for job in result.jobs} == {
        f"https://zofund.zhiye.com/campusxq?jobId={job_id}"
        for job_id in EXPECTED_JOBS
    }
