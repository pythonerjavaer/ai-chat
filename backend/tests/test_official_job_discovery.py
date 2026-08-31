"""Offline contract tests for balanced, non-AI employer list discovery."""

import hashlib
import json
import os
import urllib.request
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("OPENAI_API_KEY", "official-discovery-test-unused")
os.environ.setdefault("JWT_SECRET", "official-discovery-test-synthetic-secret")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend import recruitment_search as search
from backend.future_radar import adapters
from backend.future_radar import public_discovery as discovery
from backend.future_radar.seeds import VERIFIED_OFFICIAL_SOURCES
from backend.recruitment_watch import WatchFetchError, WatchFetchResult, normalize_html_text


ROOT = "https://careers.pddglobalhr.com/campus"
YEAR = date.today().year + (1 if date.today().month >= 6 else 0)


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("This test must not use live HTTP")

    monkeypatch.setattr(discovery, "fetch_watch_page", fail)
    monkeypatch.setattr(adapters, "fetch_watch_page", fail)
    monkeypatch.setattr(search, "fetch_watch_page", fail)
    monkeypatch.setattr(adapters.DOMAIN_LIMITER, "wait", lambda *_args: None)


def page(url, raw, final_url=None):
    text = normalize_html_text(raw)
    return WatchFetchResult(
        url=url, final_url=final_url or url, fingerprint=hashlib.sha256(text.encode()).hexdigest(),
        keyword_hits=["校园招聘"] if "校园招聘" in text else [],
        content_bytes=len(raw.encode()), http_status=200, text=text, raw_text=raw,
    )


def job_html(index=1, *, structured=False, employer="拼多多集团", year=YEAR, open=True):
    title = f"数据分析师 {index}"
    description = f"岗位职责：分析业务数据。任职要求：{year}届校园招聘毕业生。"
    script = ""
    if structured:
        script = '<script type="application/ld+json">' + json.dumps({
            "@context": "https://schema.org", "@graph": [{
                "@type": "JobPosting", "title": title,
                "description": f"<p>{description}</p>",
                "hiringOrganization": {"name": employer},
                "jobLocation": {"address": {"addressLocality": "上海"}},
                "datePosted": "2026-08-01",
                "url": f"/jobs/{index}",
            }],
        }, ensure_ascii=False) + "</script>"
    return (
        f"<h1>{title}</h1><p>{employer} {description}</p><p>工作地点：上海</p>"
        + ('<a href="/apply">立即申请</a>' if open else "") + script
    )


def listing(count=3, next_href=None, counter="", offset=0):
    return f"<h1>拼多多集团 {YEAR}届校园招聘</h1><p>共 {count} 个职位 {counter}</p>" + "".join(
        f'<a href="/jobs/{index}">数据分析师 {index}</a>' for index in range(offset + 1, count + 1)
    ) + (f'<a rel="next" href="{next_href}">下一页</a>' if next_href else "")


def fetch_pages(pages):
    calls = []

    def fetch(url, *_args, **kwargs):
        assert kwargs["timeout_seconds"] > 0
        calls.append(url)
        if url not in pages:
            pytest.fail(f"Unexpected synthetic page: {url}")
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return page(url, value)

    return fetch, calls


def test_follows_actual_pagination_and_jsonld_details_without_ai():
    next_url = ROOT + "?page=2"
    first = listing(2, "?page=2", "第1页 共2页").replace('<a href="/jobs/2">数据分析师 2</a>', "")
    second = listing(2, counter="第2页 共2页", offset=1) + '<a aria-disabled="true" href="?page=3">下一页</a>'
    fetch, calls = fetch_pages({
        ROOT: first, next_url: second,
        ROOT.replace("/campus", "/jobs/1"): job_html(1, structured=True),
        ROOT.replace("/campus", "/jobs/2"): job_html(2),
    })
    result = discovery.discover_official_job_pages([ROOT], company="拼多多集团", fetcher=fetch)
    assert len(result.candidates) == 2
    assert {job.job["city"] for job in result.candidates} == {"上海"}
    assert all(job.job["opening_date"] is None for job in result.candidates)
    assert len(calls) == 4
    assert result.coverage["status"] == "healthy"
    assert result.coverage["pagination_complete"] is True
    assert result.coverage["snapshot_complete"] is False
    source = result.coverage["sources"][0]
    assert source["listing_pages_fetched"] == source["detail_pages_fetched"] == 2


@pytest.mark.parametrize("max_lists,max_details,expected_jobs,deferred_lists,deferred_details", [
    (1, 10, 1, 1, 0), (10, 1, 1, 0, 1), (10, 10, 2, 0, 0),
])
def test_bounded_list_and_detail_budgets_are_truthful(max_lists, max_details, expected_jobs, deferred_lists, deferred_details):
    first = listing(2, "?page=2", "第1页 共2页").replace('<a href="/jobs/2">数据分析师 2</a>', "")
    fetch, _ = fetch_pages({
        ROOT: first, ROOT + "?page=2": listing(2, counter="第2页 共2页", offset=1),
        ROOT.replace("/campus", "/jobs/1"): job_html(1),
        ROOT.replace("/campus", "/jobs/2"): job_html(2),
    })
    result = discovery.discover_official_job_pages(
        [ROOT], company="拼多多", fetcher=fetch, max_listing_pages=max_lists, max_detail_pages=max_details,
    )
    assert len(result.candidates) == expected_jobs
    source = result.coverage["sources"][0]
    assert source["deferred_listing_pages"] == deferred_lists
    assert source["deferred_detail_pages"] == deferred_details
    assert source["status"] == ("partial" if deferred_lists or deferred_details else "healthy")
    assert result.coverage["snapshot_complete"] is False


def test_timeout_preserves_successful_details_and_reports_deferred_urls():
    now = [0.0]
    fetch, calls = fetch_pages({ROOT: listing(3), **{
        ROOT.replace("/campus", f"/jobs/{index}"): job_html(index) for index in range(1, 4)
    }})

    def timed_fetch(*args, **kwargs):
        result = fetch(*args, **kwargs)
        now[0] += 1
        return result

    result = discovery.discover_official_job_pages(
        [ROOT], company="拼多多", fetcher=timed_fetch, max_seconds=2, clock=lambda: now[0],
    )
    assert len(result.candidates) == 1
    assert len(calls) == 2
    assert result.coverage["completion_reason"] == "time_budget"
    assert result.coverage["sources"][0]["deferred_detail_pages"] == 2
    assert result.coverage["status"] == "partial"


def test_failed_detail_does_not_discard_its_successful_sibling():
    fetch, _ = fetch_pages({
        ROOT: listing(2), ROOT.replace("/campus", "/jobs/1"): job_html(1),
        ROOT.replace("/campus", "/jobs/2"): WatchFetchError("synthetic timeout"),
    })
    result = discovery.discover_official_job_pages([ROOT], company="拼多多", fetcher=fetch)
    assert len(result.candidates) == 1
    assert result.coverage["sources"][0]["detail_failures"] == 1
    assert result.coverage["pagination_complete"] is True  # listing, NOT detail coverage
    assert result.coverage["status"] == "partial"


@pytest.mark.parametrize("html,expected", [
    ("<h1>校园招聘</h1>", False),
    ("<h1>校园招聘</h1><button>加载更多</button>", False),
    ("<h1>职位列表</h1><p>暂无职位</p>", True),
    ("<h1>职位列表</h1><p>共 0 个职位</p><button>下一页</button>", False),
    ("<h1>职位列表</h1><p>第1页 共3页</p>", False),
])
def test_no_new_job_never_implies_full_coverage_without_list_evidence(html, expected):
    fetch, _ = fetch_pages({ROOT: html})
    result = discovery.discover_official_job_pages([ROOT], company="拼多多", fetcher=fetch)
    assert not result.candidates
    assert result.coverage["pagination_complete"] is expected
    assert result.coverage["snapshot_complete"] is False


def test_repeated_first_page_is_not_pagination_success():
    html = listing(1, "?page=2", "第1页 共2页")
    fetch, calls = fetch_pages({
        ROOT: html, ROOT + "?page=2": html,
        ROOT.replace("/campus", "/jobs/1"): job_html(),
    })
    result = discovery.discover_official_job_pages([ROOT], company="拼多多", fetcher=fetch)
    assert len(result.candidates) == 1 and len(calls) == 3
    assert result.coverage["pagination_complete"] is False
    assert result.coverage["sources"][0]["unresolved_pagination"] == 1


def test_blocks_external_private_action_and_hash_links_without_fetching_them():
    html = listing(1) + """
      <a href="https://outside.example/jobs/9">数据分析师</a>
      <a href="https://127.0.0.1/jobs/10">数据分析师</a>
      <a href="/jobs/11?token=synthetic-private">数据分析师</a>
      <a href="/jobs/12#/%2Fprivate">数据分析师</a>
      <a href="/apply">立即申请</a><a href="javascript:void(0)">下一页</a>
    """
    fetch, calls = fetch_pages({ROOT: html, ROOT.replace("/campus", "/jobs/1"): job_html()})
    result = discovery.discover_official_job_pages([ROOT], company="拼多多", fetcher=fetch)
    assert len(calls) == 2 and len(result.candidates) == 1
    assert result.coverage["status"] == "partial"
    assert result.coverage["sources"][0]["blocked_detail_links"] == 4
    assert "synthetic-private" not in json.dumps(result.coverage)


def test_cross_origin_redirect_is_rejected_before_next_request(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, "build_opener", lambda handler: handler)
    handler = discovery._same_origin_opener(("https", "careers.pddglobalhr.com"))()
    with pytest.raises(WatchFetchError):
        handler.redirect_request(urllib.request.Request(ROOT), None, 302, "", {}, "https://outside.example/jobs")


def test_jsonld_without_visible_role_is_not_a_readable_vacancy():
    raw = job_html(structured=True).replace("<h1>数据分析师 1</h1>", "<h1>{{ jobTitle }}</h1>")
    url = ROOT.replace("/campus", "/jobs/1")
    fetch, _ = fetch_pages({url: raw})
    result = discovery.discover_official_job_pages([url], company="拼多多", fetcher=fetch)
    assert not result.candidates
    assert result.coverage["sources"][0]["unparsed_detail_pages"] == 1


def test_each_company_has_an_independent_budget():
    fetch, _ = fetch_pages({ROOT: listing(3), **{
        ROOT.replace("/campus", f"/jobs/{index}"): job_html(index) for index in range(1, 4)
    }})
    first = discovery.discover_official_job_pages([ROOT], company="企业甲", fetcher=fetch, max_detail_pages=2)
    second = discovery.discover_official_job_pages([ROOT], company="企业乙", fetcher=fetch, max_detail_pages=2)
    assert len(first.candidates) == len(second.candidates) == 2
    assert first.coverage["sources"][0]["deferred_detail_pages"] == second.coverage["sources"][0]["deferred_detail_pages"] == 1


def test_every_entry_point_has_explicit_failure_or_success():
    bad = "https://careers.pddglobalhr.com/campus/other"
    fetch, _ = fetch_pages({ROOT: "<h1>暂无职位</h1>", bad: WatchFetchError("synthetic failure")})
    result = discovery.discover_official_job_pages([ROOT, bad], company="拼多多", fetcher=fetch)
    assert [item["status"] for item in result.coverage["sources"]] == ["healthy", "failed"]
    assert result.coverage["pagination_complete"] is False


def test_official_adapter_preserves_configured_jobs_and_uses_shared_verification(monkeypatch):
    source = {
        "id": "synthetic-company-source", "company": "拼多多集团", "url": ROOT,
        "domain": "careers.pddglobalhr.com", "adapter_config": {
            "discover_job_links": True, "recruitment_year": YEAR, "ai_extract": False,
            "required_markers": ["拼多多集团"],
            "job_title": "预设岗位", "job_marker": "预设岗位", "max_detail_pages": 1,
        },
    }
    fetch, _ = fetch_pages({ROOT: listing(2) + "<p>预设岗位</p>", ROOT.replace("/campus", "/jobs/1"): job_html(structured=True)})
    monkeypatch.setattr(adapters, "fetch_watch_page", fetch)
    result = adapters.OfficialHtmlAdapter(repository=None, api_key="unused", ai_model="unused").scan(source)
    assert {job["title"] for job in result.jobs} == {"预设岗位", "数据分析师 1"}
    assert all(job["verification_status"] == "verified" for job in result.jobs)
    assert result.status == "partial" and result.snapshot_complete is False
    assert result.coverage["sources"][0]["deferred_detail_pages"] == 1
    assert result.ai_calls == result.model_tokens_used == 0


@pytest.mark.parametrize("employer,cohort,open,expected", [
    ("拼多多集团", YEAR, True, "verified"),
    ("拼多多集团", YEAR, False, "pending"),
    ("拼多多集团", YEAR - 1, True, None),
    ("其他集团", YEAR, True, None),
])
def test_official_discovery_does_not_promote_other_employer_or_wrong_cohort(monkeypatch, employer, cohort, open, expected):
    source = {
        "id": "synthetic-company-source", "company": "拼多多集团", "url": ROOT,
        "adapter_config": {"discover_job_links": True, "recruitment_year": YEAR},
    }
    fetch, _ = fetch_pages({ROOT: listing(1), ROOT.replace("/campus", "/jobs/1"): job_html(structured=True, employer=employer, year=cohort, open=open)})
    monkeypatch.setattr(adapters, "fetch_watch_page", fetch)
    result = adapters.OfficialHtmlAdapter(repository=None, api_key="unused", ai_model="unused").scan(source)
    assert [job["verification_status"] for job in result.jobs] == ([expected] if expected else [])
    assert result.snapshot_complete is False


def test_all_existing_non_operator_official_seeds_opt_in_without_removing_configured_roles():
    sources = [source for source in VERIFIED_OFFICIAL_SOURCES if source["source_type"] == "official_html" and not source["company"].startswith("中国电信")]
    assert len(sources) == 6
    assert all(source["adapter_config"].get("discover_job_links") for source in sources)
    honor = next(source for source in sources if source["company"] == "荣耀")
    assert len(honor["adapter_config"]["configured_jobs"]) == 3


def test_company_search_follows_official_citation_when_model_has_no_jobs(monkeypatch):
    batch = next(batch for batch in search.build_employer_search_batches() if batch.targets[0].canonical_name == "拼多多")
    fetch, _ = fetch_pages({ROOT: listing(1), ROOT.replace("/campus", "/jobs/1"): job_html(structured=True)})
    monkeypatch.setattr(search, "fetch_watch_page", fetch)
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps({"checked_employers": [{"target_id": batch.targets[0].id}], "jobs": []}),
            output=[SimpleNamespace(type="web_search_call", status="completed", action=SimpleNamespace(sources=[SimpleNamespace(url=ROOT)]))],
            usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5), model="synthetic",
        )

    result = search._search_batch(SimpleNamespace(responses=SimpleNamespace(create=create)), batch)
    assert len(requests) == 1 and result.tool_calls == 1 and result.total_tokens == 5
    assert len(result.jobs) == 1
    assert "标题已验证" in result.jobs[0]["tags"]
    assert result.official_discovery[0]["pagination_complete"] is True
    assert result.official_discovery[0]["accepted_count"] == 1
    assert result.searched_count == result.target_count == 1


def test_search_adapter_reports_http_partial_separately_from_successful_search(monkeypatch):
    result = search.WebRecruitmentSearchResult(
        jobs=[], input_tokens=1, output_tokens=1, total_tokens=2, tool_calls=1, model="synthetic",
        target_employers=("拼多多",), searched_employers=("拼多多",), search_batches=1,
        official_discovery=({"employer": "拼多多", "status": "partial", "pagination_complete": False},),
    )
    monkeypatch.setattr(adapters, "search_current_recruitment_jobs", lambda: result)
    scanned = adapters.OpenAIWebSearchAdapter().scan({})
    assert scanned.coverage["searched_count"] == 1
    assert scanned.coverage["failed_count"] == 0
    assert scanned.coverage["official_partial_or_failed_count"] == 1
    assert scanned.coverage["official_pagination_complete_count"] == 0
    assert scanned.status == "partial" and scanned.snapshot_complete is False


def test_lost_source_lease_stops_before_another_public_request():
    cancelled = [False]
    calls = []

    def check():
        if cancelled[0]:
            raise RuntimeError("synthetic lease lost")

    def fetch(url, *_args, **_kwargs):
        calls.append(url)
        cancelled[0] = True
        return page(url, listing(3))

    with pytest.raises(discovery.OfficialDiscoveryCancelled):
        discovery.discover_official_job_pages([ROOT], company="拼多多", fetcher=fetch, cancellation_check=check)
    assert calls == [ROOT]


def test_lost_source_lease_does_not_start_hosted_search():
    batch = next(batch for batch in search.build_employer_search_batches() if batch.targets[0].canonical_name == "拼多多")

    def check():
        raise RuntimeError("synthetic lease lost")

    with pytest.raises(discovery.OfficialDiscoveryCancelled):
        search._search_batch(SimpleNamespace(), batch, cancellation_check=check)
    with pytest.raises(discovery.OfficialDiscoveryCancelled):
        search.search_current_recruitment_jobs(SimpleNamespace(), cancellation_check=check)


def test_workday_jsonld_is_typed_public_evidence_but_date_posted_is_not_application_opening():
    url = "https://mastercard.wd1.myworkdayjobs.com/en-US/External/job/Shanghai/Graduate-Analyst_R-999"
    title = f"Graduate Analyst {YEAR}"
    data = {
        "@type": "JobPosting", "title": title, "url": url,
        "hiringOrganization": {"name": "Mastercard"},
        "description": f"<p>{YEAR} campus graduates will analyse business data and deliver client research.</p>",
        "datePosted": "2026-08-01", "validThrough": "2099-01-01T00:00:00Z",
    }
    raw = f'<title>{title}</title><a href="/apply">Apply now</a><script type="application/ld+json">{json.dumps(data)}</script>'
    fetch, _ = fetch_pages({url: raw})
    result = discovery.discover_official_job_pages([url], company="Mastercard", fetcher=fetch)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.job["company"] == "Mastercard"
    assert candidate.job["opening_date"] is None and candidate.job["closing_date"] is None
    assert candidate.job["posting_expired"] is False
    evidence = search._evaluate_official_candidate_page(candidate.job, candidate.page_text, candidate.final_url)
    assert evidence.title_confirmed is True
    assert result.coverage["pagination_complete"] is False  # one detail is not the whole ATS


def test_jsonld_expiry_is_not_ignored_when_page_still_has_stale_apply_button(monkeypatch):
    html = job_html(structured=True).replace('"datePosted": "2026-08-01"', '"validThrough": "2000-01-01"')
    batch = next(batch for batch in search.build_employer_search_batches() if batch.targets[0].canonical_name == "拼多多")
    fetch, _ = fetch_pages({ROOT: listing(1), ROOT.replace("/campus", "/jobs/1"): html})
    monkeypatch.setattr(search, "fetch_watch_page", fetch)
    jobs, coverage = search._discover_company_official_jobs(batch, {ROOT}, [])
    assert not jobs
    assert coverage["candidate_decisions"]["official_posting_expired"] == 1


def test_workday_structured_only_body_yields_a_real_pending_candidate():
    url = "https://mastercard.wd1.myworkdayjobs.com/en-US/campus/job/Beijing/Graduate-Analyst_R-999"
    raw = '<script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": f"Graduate Analyst {YEAR}", "url": url,
        "hiringOrganization": {"name": "Mastercard"},
        "description": f"{YEAR} campus graduates will analyse data and deliver client research.",
    }) + "</script>"
    received_options = []

    def fetch(public_url, *_args, **kwargs):
        received_options.append(kwargs.get("allow_structured_body"))
        return page(public_url, raw)

    result = discovery.discover_official_job_pages([url], company="Mastercard", fetcher=fetch)
    assert received_options == [True]
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    evidence = search._evaluate_official_candidate_page(candidate.job, candidate.page_text, candidate.final_url)
    assert evidence.employer_confirmed and evidence.cohort_confirmed and evidence.identity_confirmed
    assert evidence.open_confirmed is False and evidence.title_confirmed is False
    assert result.coverage["snapshot_complete"] is False


def test_legacy_injected_fetcher_signature_does_not_need_new_opt_in_keyword():
    calls = []

    def fetch(url, keywords, *, timeout_seconds, max_bytes, opener_factory):
        calls.append(url)
        return page(url, "<h1>暂无职位</h1>")

    result = discovery.discover_official_job_pages([ROOT], company="拼多多", fetcher=fetch)
    assert calls == [ROOT]
    assert result.coverage["pagination_complete"] is True


@pytest.mark.parametrize("url,accepted", [
    ("https://myjob.hzbank.com.cn/hzzp-apply-web/static/index.html", True),
    ("https://mastercard.wd1.myworkdayjobs.com/en-US/campus/job/Beijing/Associate-Analyst--Account-Management_R-999", True),
    ("https://careers.example.com/apply", False),
    ("https://careers.example.com/account/settings", False),
    ("https://careers.example.com/login.html", False),
])
def test_action_endpoint_filter_does_not_reject_static_apps_or_actual_job_slugs(url, accepted):
    assert bool(discovery._official_link(url, url)) is accepted
