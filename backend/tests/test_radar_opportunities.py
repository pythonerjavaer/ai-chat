"""The main pool includes safe campus discoveries without claiming verification."""

import json
import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_WEB_SEARCH_ENABLED", "false")

from fastapi.testclient import TestClient

from backend import database, main
from backend.future_radar.normalization import normalize_job
from backend.future_radar.repository import utc_now
from backend.future_radar.service import FutureRadarService
from backend.live_sources import is_actionable_recruitment_listing, is_recruitment_program_listing
from backend.recruitment import score_job
from backend.recruitment_watch import WatchFetchError


DISCOVERY = "legacy-search-discovery"
OFFICIAL = "legacy-recruitment-pipeline"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "settings", SimpleNamespace(
        database_path=tmp_path / "opportunities.db"
    ))
    options = vars(main.settings).copy()
    options.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
        "recruitment_ingest_token": "test-recruitment-ingest-token",
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**options))

    def no_network(_source):
        raise AssertionError("Opportunity reads must never invoke source adapters")

    service = FutureRadarService(
        connect=database.connect, openai_api_key="test-key", ai_model="test-model",
        web_search_enabled=False, adapter_factory=no_network,
    )
    monkeypatch.setattr(main, "future_radar_service", service)

    def insert(key, *, source_id=DISCOVERY, **overrides):
        raw = {
            "external_id": key, "company": "示例科技",
            "title": f"2027 校园招聘数据分析岗 {key}", "city": "上海",
            "region": "中国大陆", "employer_type": "互联网企业", "industry": "科技",
            "primary_category": "internet_tech", "status": "open",
            "verification_status": "pending", "tags": ["校园招聘", "2027届"],
            "official_url": f"https://careers.example.com/campus/{key}",
            "description": "负责业务分析和数据研究，支持业务决策，参与构建经营指标体系。",
            "responsibilities": "分析经营数据，参与研究业务问题，构建数据看板和指标体系。",
            "requirements": "面向应届毕业生，能够进行数据分析和研究。",
            **overrides,
        }
        item = normalize_job(raw)
        source = service.repository.get_source(source_id)
        with service.repository.transaction() as connection:
            saved = service.repository.insert_job(
                connection, item, source_id=source_id, program_id=None, now=utc_now(),
            )
            service.repository.link_job_source(
                connection, job_id=saved["id"], source=source,
                source_url=item.get("official_url"), now=utc_now(),
                verification_role="verification" if source_id == OFFICIAL else "discovery",
                evidence=["PRIVATE_EVIDENCE_DO_NOT_EXPOSE"],
            )
        return saved

    with TestClient(main.app) as client:
        registration = client.post("/api/auth/register", json={
            "username": "opportunity-user", "password": "correct-horse-123",
            "privacy_accepted": True,
        })
        assert registration.status_code == 201
        auth = {"Authorization": f"Bearer {registration.json()['access_token']}"}
        yield SimpleNamespace(client=client, auth=auth, service=service, insert=insert)


def get_pool(harness, **params):
    response = harness.client.get(
        "/api/future-radar/opportunities", params=params, headers=harness.auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_pending_and_conflicted_campus_discoveries_are_immediately_visible(harness):
    pending = harness.insert("new-role", closing_date=None)
    harness.insert("conflicting-role", verification_status="conflicted")
    result = get_pool(harness)
    assert result["pool"] == "opportunities"
    assert result["total"] == 2
    assert result["items"] == result["jobs"] == result["opportunities"]
    assert {item["verification_status"] for item in result["items"]} == {"pending", "conflicted"}
    assert all(item["available_in_main_pool"] for item in result["items"])
    assert all(item["opportunity_kind"] == "discovered" for item in result["items"])
    assert not any(item["officially_verified"] for item in result["items"])
    assert result["stats"]["discovered_count"] == 2
    assert harness.service.repository.get_job(pending["id"])["verification_status"] == "pending"
    assert harness.client.get("/api/future-radar/jobs", headers=harness.auth).json()["total"] == 0
    detail = harness.client.get(
        f"/api/future-radar/opportunities/{pending['id']}", headers=harness.auth,
    )
    assert detail.status_code == 200
    assert detail.json()["verification_status"] == "pending"
    assert "tier_bucket" in detail.json()


def test_official_and_discovery_duplicates_merge_without_changing_verification(harness):
    pending = harness.insert("discovery-copy", title="2027 校园招聘 数据分析岗", city="上海市")
    official = harness.insert(
        "official-copy", source_id=OFFICIAL, title="2027 校园招聘数据分析岗", city="上海",
        verification_status="verified",
    )
    result = get_pool(harness)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["id"] == official["id"]
    assert item["verification_status"] == "verified"
    assert {source["source_id"] for source in item["sources"]} == {DISCOVERY, OFFICIAL}
    assert len(item["discovered_by"]) == len(item["verified_by"]) == 1
    alias = harness.client.get(
        f"/api/future-radar/opportunities/{pending['id']}", headers=harness.auth,
    )
    assert alias.status_code == 200
    assert alias.json()["id"] == official["id"]


def test_shared_campaign_urls_do_not_collapse_distinct_roles_or_cohorts(harness):
    url = "https://careers.example.com/campus/2027"
    harness.insert("one", title="2027 校园招聘数据分析岗", official_url=url)
    harness.insert("two", title="2027 校园招聘产品经理", official_url=url)
    harness.insert("three", title="2028 校园招聘数据分析岗", official_url=url, tags=["2028届"])
    assert get_pool(harness)["total"] == 3


def test_known_employer_aliases_share_one_opportunity_without_substring_merges(harness):
    harness.insert("alias-cn", company="腾讯", title="2027 校园招聘数据分析岗")
    harness.insert("alias-en", company="Tencent", title="2027 校园招聘数据分析岗")
    harness.insert("different-employer", company="腾讯云合作伙伴", title="2027 校园招聘数据分析岗")
    assert get_pool(harness)["total"] == 2


@pytest.mark.parametrize("tier, bucket", [(None, "UNRANKED"), ("不建议投", "BELOW_PRIORITY")])
def test_unranked_and_below_priority_filters_keep_existing_score_meaning(harness, monkeypatch, tier, bucket):
    harness.insert("unranked-role")
    monkeypatch.setattr(main, "score_job", lambda job, _profile: {**job, "tier_code": tier})
    result = get_pool(harness, tier_code=bucket)
    assert result["total"] == result["stats"]["tier_counts"][bucket] == 1
    assert result["items"][0]["tier_code"] == tier
    assert result["items"][0]["tier_bucket"] == bucket


@pytest.mark.parametrize("overrides", [
    {"status": "closed"},
    {"verification_status": "rejected"},
    {"closing_date": (date.today() - timedelta(days=1)).isoformat()},
    {"closing_date": date.today().isoformat()},
    {"title": "社会招聘 数据分析岗"},
    {"title": "Experienced hire: Data analyst"},
    {"title": "数据分析岗", "tags": [], "description": "", "responsibilities": "", "requirements": "3年以上工作经验"},
    {"official_url": None, "application_url": None},
    {"official_url": "https://chatgpt.com/c/private-example"},
])
def test_closed_expired_rejected_non_campus_and_private_links_are_not_in_main_pool(harness, overrides):
    item = harness.insert("hidden", **overrides)
    assert get_pool(harness)["total"] == 0
    archived = overrides.get("status") == "closed" or "closing_date" in overrides
    assert harness.client.get(
        f"/api/future-radar/opportunities/{item['id']}", headers=harness.auth,
    ).status_code == (200 if archived else 404)


@pytest.mark.parametrize("status", ["closed", "unknown"])
def test_explicit_archive_filter_results_have_working_details(harness, status):
    saved = harness.insert(f"archive-{status}", status=status)
    assert get_pool(harness)["total"] == 0
    archived = get_pool(harness, status=status)
    assert archived["total"] == 1
    detail = harness.client.get(
        f"/api/future-radar/opportunities/{saved['id']}", headers=harness.auth,
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == archived["items"][0]["id"]
    assert detail.json()["status"] == status
    assert detail.json()["verification_status"] == "pending"


def test_closed_verified_record_cannot_be_resurrected_by_stale_open_copy(harness):
    harness.insert("stale", title="2027 校园招聘数据分析岗")
    harness.insert(
        "closed-official", source_id=OFFICIAL, title="2027 校园招聘数据分析岗",
        status="closed", verification_status="verified",
    )
    assert get_pool(harness)["total"] == 0
    archived = get_pool(harness, status="closed")
    assert archived["total"] == 1
    assert archived["items"][0]["status"] == "closed"


def test_source_and_verification_filters_cannot_resurrect_closed_official_duplicate(harness):
    harness.insert("stale-filtered", title="2027 校园招聘数据分析岗")
    harness.insert(
        "official-filtered", source_id=OFFICIAL, title="2027 校园招聘数据分析岗",
        status="closed", verification_status="verified",
    )
    assert get_pool(harness, source_id=DISCOVERY)["total"] == 0
    assert get_pool(harness, verification_status="pending")["total"] == 0
    archived = get_pool(harness, status="closed", source_id=DISCOVERY)
    assert archived["total"] == 1
    assert archived["items"][0]["verification_status"] == "verified"
    assert {source["source_id"] for source in archived["items"][0]["sources"]} == {DISCOVERY, OFFICIAL}


def test_source_filter_keeps_merged_authoritative_record_and_all_provenance(harness):
    harness.insert("open-lead", title="2027 校园招聘数据分析岗")
    official = harness.insert(
        "open-official", source_id=OFFICIAL, title="2027 校园招聘数据分析岗",
        verification_status="verified",
    )
    result = get_pool(harness, source_id=DISCOVERY)
    assert result["total"] == 1
    assert result["items"][0]["id"] == official["id"]
    assert result["stats"]["verified_count"] == 1
    assert result["stats"]["discovered_count"] == 0


def test_category_counts_pagination_and_tier_filter_cover_all_results(harness, monkeypatch):
    for index in range(57):
        harness.insert(
            f"role-{index:03d}", primary_category="internet_tech",
            company="示例科技" if index < 55 else "另一科技",
        )
    harness.insert("other-category", company="示例保险", primary_category="insurance_integrated_finance")

    def predictable_score(job, _profile):
        tier = "T1" if job["external_id"] in {"role-055", "role-056"} else "T2"
        return {**job, "tier_code": tier, "scoring_status": "scored"}

    monkeypatch.setattr(main, "score_job", predictable_score)
    result = get_pool(harness, category="internet_tech", page_size=50, sort="company")
    assert result["total"] == 57
    assert len(result["items"]) == 50
    assert result["stats"]["tier_counts"]["T1"] == 2
    assert result["stats"]["tier_counts"]["T2"] == 55
    assert result["stats"]["category_counts"] == {"internet_tech": 57}
    filtered = get_pool(harness, category="internet_tech", tier_code="T1", page_size=1)
    assert filtered["total"] == 2
    assert len(filtered["items"]) == 1
    assert filtered["stats"]["matching_total"] == 57
    assert filtered["stats"]["tier_counts"]["T2"] == 55
    second = get_pool(harness, category="internet_tech", tier_code="T1", page_size=1, page=2)
    assert second["total"] == 2
    assert second["items"][0]["id"] != filtered["items"][0]["id"]
    combined = get_pool(harness, category=["internet_tech", "insurance_integrated_finance"])
    assert combined["total"] == 58
    assert sum(combined["stats"]["category_counts"].values()) == 58


def test_existing_scoring_rules_are_used_without_fabricated_tiers(harness):
    saved = harness.insert("scorable")
    job = harness.service.repository.get_job(saved["id"])
    expected = score_job(main._public_search_update(job), {})
    actual = get_pool(harness)["items"][0]
    assert actual["tier_code"] == expected["tier_code"]
    assert actual["scoring_version"] == expected["scoring_version"]
    assert actual["job_score"] == expected["job_score"]
    harness.insert("no-jd", title="2027 校园招聘", description="", responsibilities="", requirements="")
    # A real employer + current cohort + recruitment URL makes a program lead,
    # not an invented scored vacancy, even when no detailed JD exists yet.
    result = get_pool(harness)
    assert result["total"] == 2
    project = next(item for item in result["items"] if item["external_id"] == "no-jd")
    assert project["listing_kind"] == "recruitment_program"
    assert project["is_specific_job"] is False
    assert project["tier_code"] is None
    assert project["scoring_status"] == "unscored_program_listing"


@pytest.mark.parametrize("company,title", [
    ("招银网络科技", "2027秋季校园招聘"),
    ("联通支付", "联通支付2027届校园招聘"),
    ("徽商银行", "2027徽星计划管理培训生"),
    ("招商银行成都分行", "招商银行成都分行2027秋季校招"),
])
def test_current_employer_recruitment_programs_are_actionable_and_visible_as_programs(harness, company, title):
    item = {
        "company": company, "title": title,
        "official_url": "https://careers.example.com/campus/2027",
        "requirements": "", "tags": [],
    }
    assert is_actionable_recruitment_listing(item)
    assert is_recruitment_program_listing(item)
    harness.insert("company-project", **item, description="", responsibilities="")
    pool = get_pool(harness)
    assert pool["total"] == 1
    assert pool["items"][0]["listing_kind"] == "recruitment_program"
    assert pool["items"][0]["is_specific_job"] is False
    assert pool["items"][0]["tier_code"] is None
    assert pool["items"][0]["title"] == title


@pytest.mark.parametrize("company", ["广州招聘", "校园招聘", "央国企招聘", "银行招聘网", "广州", ""])
def test_navigation_or_missing_employer_never_becomes_a_program(company):
    item = {
        "company": company, "title": "2027校园招聘", "tags": ["校园招聘"],
        "official_url": "https://careers.example.com/campus/2027",
    }
    assert not is_actionable_recruitment_listing(item)
    assert not is_recruitment_program_listing(item)


def test_program_requires_current_cohort_and_safe_public_reference():
    base = {"company": "招银网络科技", "title": "2027秋季校园招聘", "tags": []}
    for url in (None, "file:///private/file", "http://careers.example.com/", "https://127.0.0.1/", "https://chatgpt.com/c/private-example"):
        assert not is_actionable_recruitment_listing({**base, "official_url": url})
    assert not is_actionable_recruitment_listing({
        **base, "title": "2025秋季校园招聘", "official_url": "https://careers.example.com/campus/2025",
    })


def test_program_ingest_preserves_company_url_and_bridges_it_without_official_page_readability(harness, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise WatchFetchError("JS page body not available")

    monkeypatch.setattr(main, "fetch_watch_page", unavailable)
    # Only the internal legacy DB projection is allowed in this test.
    from backend.future_radar.adapters import LegacyDatabaseAdapter
    harness.service.adapter_factory = lambda source: LegacyDatabaseAdapter() if source["id"] == DISCOVERY else None
    result = harness.client.post("/api/recruitment/ingest", headers={
        "X-Recruitment-Token": "test-recruitment-ingest-token",
    }, json={
        "source_id": "chatgpt-radar-03",
        "jobs": [{
            "external_id": "real-company-program",
            "company": "招银网络科技", "title": "2027秋季校园招聘",
            "city": "待具体岗位确认",
            "official_url": "https://careers.example.com/campus/2027",
            "tags": [],
        }],
    })
    assert result.status_code == 200
    assert result.json()["pending"] == 1
    assert result.json()["rejected"] == 0
    assert result.json()["search_updates_refresh"] == {"status": "success"}
    pool = get_pool(harness)
    assert pool["total"] == 1
    assert pool["items"][0]["company"] == "招银网络科技"
    assert pool["items"][0]["listing_kind"] == "recruitment_program"
    assert pool["items"][0]["verification_status"] == "pending"


def test_specific_role_with_campus_suffix_is_not_downgraded_to_a_program(harness):
    harness.insert("real-role", company="招银网络科技", title="2027数据分析岗校园招聘")
    result = get_pool(harness)
    assert result["total"] == 1
    assert result["items"][0]["listing_kind"] == "job"
    assert result["items"][0]["is_specific_job"] is True


def test_main_pool_and_details_remain_jwt_protected(harness):
    saved = harness.insert("protected")
    for path in (
        "/api/future-radar/opportunities",
        f"/api/future-radar/opportunities/{saved['id']}",
    ):
        assert harness.client.get(path).status_code == 401
        assert harness.client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_provenance_and_coverage_never_expose_private_transport_fields(harness):
    marker = "-".join(("12345678", "1234", "4123", "8123", "123456789abc"))
    saved = harness.insert("safe-reference", description=f"校园招聘数据分析。test@example.com {marker} 13800138000")
    harness.service.repository.save_snapshot("openai-public-web-search", "safe-snapshot", "", {
        "coverage": {
            "target_count": 205, "searched_count": 200, "failed_count": 5,
            "failed_employers": [f"示例公司 test@example.com {marker}"],
            "cookie": "PRIVATE_COOKIE_DO_NOT_EXPOSE", "source_thread_id": marker,
        },
    })
    result = get_pool(harness)
    assert result["scope"]["list_entry_count"] == 218
    assert result["scope"]["target_count"] == 205
    assert result["coverage"]["searched_count"] == 200
    text = json.dumps(result)
    detail = harness.client.get(
        f"/api/future-radar/opportunities/{saved['id']}", headers=harness.auth,
    )
    for secret in (marker, "test@example.com", "13800138000", "PRIVATE_EVIDENCE", "PRIVATE_COOKIE", "source_thread_id"):
        assert secret not in text
        assert secret not in detail.text
    assert "evidence" not in result["items"][0]["sources"][0]


def test_all_standard_query_filters_are_applied(harness):
    harness.insert("wanted", opening_date="2026-08-01", closing_date="2099-09-01")
    harness.insert("wrong-city", city="北京")
    result = get_pool(
        harness, city="上海", company="示例科技", source_id=DISCOVERY,
        region="中国大陆", employer_type="互联网企业", industry="科技", q="wanted",
        opening_after="2026-07-01", opening_before="2026-09-01",
        closing_after="2099-08-01", closing_before="2099-10-01",
    )
    assert result["total"] == 1
    assert result["items"][0]["external_id"] == "wanted"
    assert get_pool(harness, category="policy_state_banks")["total"] == 0
    for params in ({"category": "invalid-category"}, {"tier_code": "T99"}):
        assert harness.client.get(
            "/api/future-radar/opportunities", params=params, headers=harness.auth,
        ).status_code == 422
