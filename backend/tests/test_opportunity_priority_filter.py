"""Priority is a reversible display projection, tested with synthetic SQLite rows.

No application lifespan, real account, PostgreSQL, network, or configured DB.
The large fixture is inserted in one transaction, not 2,700 separate commits.
"""

from __future__ import annotations

import inspect
import os
from types import SimpleNamespace
from typing import get_type_hints

import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "unused-priority-filter-test-key")
os.environ.setdefault("JWT_SECRET", "isolated-priority-filter-test-secret-32-bytes")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend.future_radar.normalization import PRIMARY_CATEGORY_CODES
from backend.future_radar.repository import utc_now
from backend.tests.test_opportunity_company_groups import pool  # noqa: F401 - shared SQLite fixture


ELIGIBLE_TIERS = ("T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3", None)
TELECOM = "state_tech_telecom"
TECH = "internet_tech"
FINANCE = "insurance_integrated_finance"
BANKS = "policy_state_banks"
BIG_FOUR = "big_four_professional_services"


@pytest.fixture
def priority_pool(pool):
    repository = pool.repository
    scores = {}
    kinds = {}
    job_scores = {}
    changed_at = {}
    calls = []
    sources = {key: repository.get_source(key) for key in ("public-fixture", "other-fixture")}

    def insert_many(specifications):
        now = utc_now()
        with repository.transaction() as connection:
            for specification in specifications:
                specification = dict(specification)
                key = specification.pop("key")
                scores[key] = specification.pop("tier", "T2")
                kinds[key] = specification.pop("listing_kind", "job")
                job_scores[key] = specification.pop("job_score", None)
                changed_at[key] = specification.pop("changed_at", None)
                source_id = specification.pop("source_id", "public-fixture")
                # Synthetic serials such as 2000 must not look like cohorts to
                # the existing title identity normalizer and collapse 100 jobs.
                title_key = key.translate(str.maketrans("0123456789", "abcdefghij"))
                item = {
                    "external_id": key, "company": "合成测试企业",
                    "title": f"2027校园招聘数据分析岗 {title_key}", "city": "上海",
                    "region": "中国大陆", "primary_category": TECH,
                    "status": "unknown", "verification_status": "pending",
                    "requirements": "面向2027届毕业生的合成公开测试岗位。",
                    "official_url": f"https://careers.example.invalid/campus/{key}",
                    "opening_date": None, "closing_date": None,
                    "tags": ["校园招聘", "2027届"], "content_hash": key,
                    **specification,
                }
                saved = repository.insert_job(
                    connection, item, source_id=source_id, program_id=None, now=now,
                )
                repository.link_job_source(
                    connection, job_id=saved["id"], source=sources[source_id],
                    source_url=item["official_url"], now=now,
                    verification_role="discovery" if source_id == "public-fixture" else "verification",
                )

    def prepare(row):
        calls.append(row["external_id"])
        result = {key: row.get(key) for key in (
            "id", "external_id", "company", "title", "city", "primary_category",
            "status", "verification_status", "official_url", "opening_date", "closing_date",
            "last_changed_at", "sources",
        )} | {
            "tier_code": scores[row["external_id"]],
            "listing_kind": kinds[row["external_id"]],
            "is_specific_job": kinds[row["external_id"]] != "recruitment_program",
            "job_score": job_scores[row["external_id"]],
        }
        if changed_at[row["external_id"]] is not None:
            result["last_changed_at"] = changed_at[row["external_id"]]
        return result

    def get(*, filters=None, **kwargs):
        return repository.list_opportunities(
            public_url=pool.public_url, prepare=prepare,
            cache_scope="isolated-priority-filter-fixture",
            filters={"status": "active", **(filters or {})}, **kwargs,
        )

    return SimpleNamespace(
        repository=repository, insert_many=insert_many, get=get,
        prepare=prepare, public_url=pool.public_url, calls=calls,
    )


def test_large_priority_projection_filters_before_grouping_and_paging_without_deleting_rows(priority_pool):
    p = priority_pool
    secondary = [{
        "key": f"secondary-{index:04d}", "tier": "不建议投",
        "company": "中国联合网络通信集团有限公司", "primary_category": TELECOM,
    } for index in range(2500)]
    eligible = [{
        "key": f"eligible-{index:03d}", "tier": ELIGIBLE_TIERS[index % 8],
        "company": f"合成企业{index // 4:03d}",
        "primary_category": TECH if index % 2 == 0 else FINANCE,
        "listing_kind": "recruitment_program" if index % 16 == 15 else "job",
    } for index in range(200)]
    p.insert_many(secondary + eligible)

    all_jobs = p.get(page_size=50)
    all_companies = p.get(page_size=20, filters={"view": "companies"})
    assert all_jobs["total"] == all_jobs["total_opportunities"] == 2700
    assert all_companies["total"] == all_companies["total_companies"] == 51
    assert all_companies["total_opportunities"] == 2700
    assert all_jobs["stats"]["priority_total"] == 200
    assert all_jobs["stats"]["secondary_total"] == 2500
    assert all_jobs["stats"]["category_counts"] == {TELECOM: 2500, TECH: 100, FINANCE: 100}
    assert all_jobs["stats"]["visible_category_company_counts"] == {TELECOM: 1, TECH: 50, FINANCE: 50}

    job_pages = [p.get(page=page, page_size=50, filters={"priority_only": True}) for page in range(1, 5)]
    jobs = [row for page in job_pages for row in page["items"]]
    assert len(jobs) == len({row["id"] for row in jobs}) == 200
    assert all(row["external_id"].startswith("eligible-") for row in jobs)
    assert {row["tier_bucket"] for row in jobs} == {"T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3", "UNRANKED"}
    assert sum(row["listing_kind"] == "recruitment_program" for row in jobs) == 12
    assert all(page["total"] == 200 and len(page["items"]) == 50 for page in job_pages)

    company_pages = [p.get(page=page, page_size=17, filters={
        "view": "companies", "priority_only": True,
    }) for page in range(1, 4)]
    companies = [row for page in company_pages for row in page["items"]]
    assert [len(page["items"]) for page in company_pages] == [17, 17, 16]
    assert len(companies) == len({row["company_key"] for row in companies}) == 50
    assert sum(row["opportunity_count"] for row in companies) == 200
    assert not any(row["grouping"] == "telecom_group" for row in companies)
    for result in [*job_pages, *company_pages]:
        assert result["total_opportunities"] == 200
        assert result["total_companies"] == 50
        stats = result["stats"]
        assert stats["priority_total"] == 200 and stats["secondary_total"] == 2500
        assert stats["tier_counts"] == all_jobs["stats"]["tier_counts"]
        assert stats["category_counts"] == all_jobs["stats"]["category_counts"]
        assert stats["visible_category_counts"] == {TECH: 100, FINANCE: 100}
        # Each of these 50 companies has both categories: this is not 100 companies.
        assert stats["visible_category_company_counts"] == {TECH: 50, FINANCE: 50}

    below = p.get(page=50, page_size=50, filters={"priority_only": False, "tier_code": "BELOW_PRIORITY"})
    assert below["total"] == 2500 and len(below["items"]) == 50
    assert all(row["tier_bucket"] == "BELOW_PRIORITY" for row in below["items"])
    below_companies = p.get(filters={"view": "companies", "tier_code": "BELOW_PRIORITY"})
    assert below_companies["total"] == 1 and below_companies["items"][0]["opportunity_count"] == 2500
    assert p.get(filters={"priority_only": False})["total"] == 2700
    with p.repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 2700
        assert connection.execute(
            "SELECT COUNT(*) FROM radar_jobs WHERE external_id LIKE 'secondary-%' "
            "AND status='unknown' AND verification_status='pending'"
        ).fetchone()[0] == 2500
        assert connection.execute("SELECT COUNT(*) FROM job_sources WHERE active=1").fetchone()[0] == 2700


@pytest.mark.parametrize("tier", ["T0", "T0.5", "T1", "T1.5", "T2", "T2.5", "T3", "UNRANKED", "BELOW_PRIORITY"])
def test_priority_retains_every_eligible_tier_and_is_conjunctive_with_below(priority_pool, tier):
    p = priority_pool
    p.insert_many([{"key": f"tier-{index}", "tier": value} for index, value in enumerate((*ELIGIBLE_TIERS, "不建议投"))])
    all_tier = p.get(filters={"tier_code": tier})
    assert all_tier["total"] == 1 and all_tier["items"][0]["tier_bucket"] == tier
    for view in ("jobs", "companies"):
        result = p.get(filters={"priority_only": True, "tier_code": tier, "view": view})
        expected = 0 if tier == "BELOW_PRIORITY" else 1
        assert result["total"] == result["total_opportunities"] == expected
        assert result["total_companies"] == expected
        stats = result["stats"]
        assert stats["priority_total"] == 8 and stats["secondary_total"] == 1
        assert stats["tier_counts"] == all_tier["stats"]["tier_counts"]
        assert stats["category_counts"] == {TECH: 9}
        assert stats["visible_category_counts"] == ({TECH: 1} if expected else {})
        assert stats["visible_category_company_counts"] == ({TECH: 1} if expected else {})


def test_company_expansion_applies_base_filters_then_priority_and_tier(priority_pool):
    p = priority_pool
    root = "中国联合网络通信集团有限公司"
    branch = "中国联合网络通信有限公司安徽省分公司"
    common = {"company": root, "primary_category": TELECOM}
    p.insert_many([
        {**common, "key": "scope-hit-ranked", "tier": "T1"},
        {**common, "key": "scope-hit-unranked", "tier": None, "company": branch, "primary_category": TECH},
        {**common, "key": "scope-hit-secondary", "tier": "不建议投"},
        {**common, "key": "scope-hit-other-city", "tier": "T0", "city": "北京"},
        {**common, "key": "scope-hit-other-source", "tier": "T0", "source_id": "other-fixture", "verification_status": "verified"},
        {**common, "key": "different-search", "tier": "T0"},
        {**common, "key": "scope-hit-closed", "tier": "T0", "status": "closed"},
        {**common, "key": "scope-hit-other-category", "tier": "T0", "primary_category": FINANCE},
        {**common, "key": "scope-hit-other-employer", "tier": "T0", "company": "另一家合成科技企业"},
    ])
    filters = {
        "priority_only": True, "status": "active", "active_only": True,
        "source_id": "public-fixture", "city": "上海", "q": "scope-hit",
        "primary_categories": [TELECOM, TECH],
    }
    groups = p.get(filters={**filters, "view": "companies"})
    assert groups["total_companies"] == 2 and groups["total_opportunities"] == 3
    carrier = next(row for row in groups["items"] if row["grouping"] == "telecom_group")
    assert carrier["opportunity_count"] == 2
    expanded = p.get(filters={**filters, "company_key": carrier["company_key"], "tier_code": "UNRANKED"})
    assert expanded["total"] == expanded["total_opportunities"] == expanded["total_companies"] == 1
    assert expanded["items"][0]["company"] == branch
    assert expanded["items"][0]["external_id"] == "scope-hit-unranked"
    stats = expanded["stats"]
    assert stats["priority_total"] == 2 and stats["secondary_total"] == 1
    assert stats["category_counts"] == {TELECOM: 2, TECH: 1}
    assert stats["tier_counts"]["T1"] == stats["tier_counts"]["UNRANKED"] == stats["tier_counts"]["BELOW_PRIORITY"] == 1
    assert stats["visible_category_counts"] == {TECH: 1}
    assert stats["visible_category_company_counts"] == {TECH: 1}
    assert p.get(filters={**filters, "company_key": carrier["company_key"], "tier_code": "BELOW_PRIORITY"})["total"] == 0
    secondary = p.get(filters={**filters, "priority_only": False, "company_key": carrier["company_key"], "tier_code": "BELOW_PRIORITY"})
    assert secondary["total"] == 1 and secondary["items"][0]["external_id"] == "scope-hit-secondary"


def test_balanced_projection_rotates_ten_starfields_and_keeps_full_pool_reversible(priority_pool):
    p = priority_pool
    eligible = [
        {
            "key": f"balanced-{category_index:02d}-{index:02d}",
            "tier": "T2", "job_score": 70,
            "company": f"均衡企业-{category_index:02d}-{index:02d}",
            "primary_category": category,
        }
        for category_index, category in enumerate(PRIMARY_CATEGORY_CODES)
        for index in range(65)
    ]
    secondary = [
        {
            "key": f"balanced-secondary-{category_index:02d}",
            "tier": "不建议投",
            "company": f"次级企业-{category_index:02d}",
            "primary_category": category,
        }
        for category_index, category in enumerate(PRIMARY_CATEGORY_CODES)
    ]
    unclassified = [{
        "key": "balanced-unclassified", "tier": "T1", "job_score": 80,
        "company": "未分类但可查看企业", "primary_category": "",
    }]
    p.insert_many(eligible + secondary + unclassified)

    complete = p.get(page_size=700)
    assert complete["total"] == 661
    assert complete["stats"]["selection_mode"] == "all"
    assert complete["stats"]["priority_total"] == 651
    assert complete["stats"]["secondary_total"] == 10
    assert complete["stats"]["balanced_total"] == 240
    assert complete["stats"]["balanced_excluded_total"] == 411

    balanced = p.get(page_size=700, filters={"balanced_only": True})
    assert balanced["total"] == balanced["total_opportunities"] == 240
    assert balanced["stats"]["selection_mode"] == "balanced"
    assert balanced["stats"]["visible_category_counts"] == {
        category: 24 for category in PRIMARY_CATEGORY_CODES
    }
    assert balanced["stats"]["category_counts"] == {
        **{category: 66 for category in PRIMARY_CATEGORY_CODES}, "uncategorized": 1,
    }
    assert [item["primary_category"] for item in balanced["items"][:10]] == list(PRIMARY_CATEGORY_CODES)
    company_page = p.get(page_size=10, filters={"balanced_only": True, "view": "companies"})
    assert company_page["company_sort"] == "balanced"
    assert [next(iter(item["category_counts"])) for item in company_page["items"]] == list(PRIMARY_CATEGORY_CODES)
    assert all(item["tier_bucket"] != "BELOW_PRIORITY" for item in balanced["items"])
    assert p.get(filters={"priority_only": True})["total"] == 651
    assert any(
        item["external_id"] == "balanced-unclassified"
        for item in p.get(page_size=700, filters={"priority_only": True})["items"]
    )
    assert p.get(filters={"tier_code": "BELOW_PRIORITY"})["total"] == 10
    assert p.get(filters={"balanced_only": True, "tier_code": "BELOW_PRIORITY"})["total"] == 0
    with p.repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 661
    assert len(p.calls) == 661, "balanced/all/tier projections must reuse one scored base pool"


def test_balanced_rotates_companies_inside_one_starfield(priority_pool):
    p = priority_pool
    p.insert_many([
        {
            "key": f"alpha-{index}", "tier": "T0", "job_score": 95 - index,
            "company": "Alpha", "primary_category": TECH,
        }
        for index in range(3)
    ] + [
        {
            "key": f"beta-{index}", "tier": "T1", "job_score": 80 - index,
            "company": "Beta", "primary_category": TECH,
        }
        for index in range(3)
    ])
    result = p.get(page_size=20, filters={"balanced_only": True})
    assert [item["company"] for item in result["items"]] == [
        "Alpha", "Beta", "Alpha", "Beta", "Alpha", "Beta",
    ]


def test_balanced_rank_uses_stable_id_as_final_tie_breaker():
    from backend.future_radar.repository import RadarRepository

    common = {
        "tier_bucket": "T1", "job_score": 80, "verification_status": "verified",
        "listing_kind": "job", "last_changed_at": "2026-01-01T00:00:00+00:00",
    }
    assert RadarRepository._balanced_rank_key({**common, "external_id": "a"}) < \
        RadarRepository._balanced_rank_key({**common, "external_id": "b"})


def test_balanced_company_cap_uses_rank_order_and_expansion_reuses_global_selection(priority_pool):
    p = priority_pool
    common = {"company": "中国联通", "primary_category": TELECOM}
    p.insert_many([
        {**common, "key": "rank-01-tier", "tier": "T0", "job_score": 1,
         "listing_kind": "recruitment_program", "changed_at": "2026-01-01T00:00:00+00:00"},
        {**common, "key": "rank-02-tier", "tier": "T0.5", "job_score": 100,
         "verification_status": "verified", "source_id": "other-fixture"},
        {**common, "key": "rank-03-newer", "tier": "T1", "job_score": 90,
         "verification_status": "verified", "source_id": "other-fixture",
         "changed_at": "2026-02-01T00:00:00+00:00"},
        {**common, "key": "rank-04-older", "tier": "T1", "job_score": 90,
         "verification_status": "verified", "source_id": "other-fixture",
         "changed_at": "2026-01-01T00:00:00+00:00"},
        {**common, "key": "rank-05-program", "tier": "T1", "job_score": 90,
         "verification_status": "verified", "source_id": "other-fixture",
         "listing_kind": "recruitment_program", "changed_at": "2026-03-01T00:00:00+00:00"},
        {**common, "key": "rank-06-pending", "tier": "T1", "job_score": 90,
         "changed_at": "2026-04-01T00:00:00+00:00"},
        {**common, "key": "rank-07-score", "tier": "T1", "job_score": 80,
         "verification_status": "verified", "source_id": "other-fixture"},
        {**common, "key": "rank-08-tier", "tier": "T2", "job_score": 100,
         "verification_status": "verified", "source_id": "other-fixture"},
    ])

    balanced = p.get(page_size=20, filters={"balanced_only": True})
    assert [item["external_id"] for item in balanced["items"]] == [
        "rank-01-tier", "rank-02-tier", "rank-03-newer",
    ]
    assert balanced["stats"]["balanced_total"] == 3
    assert balanced["stats"]["balanced_excluded_total"] == 5
    groups = p.get(filters={"balanced_only": True, "view": "companies"})
    assert groups["total_companies"] == 1 and groups["items"][0]["opportunity_count"] == 3
    key = groups["items"][0]["company_key"]
    expanded = p.get(page_size=20, filters={"balanced_only": True, "company_key": key})
    assert [item["external_id"] for item in expanded["items"]] == [
        item["external_id"] for item in balanced["items"]
    ]
    assert expanded["stats"]["priority_total"] == 8
    assert expanded["stats"]["balanced_total"] == 3


def test_balanced_projection_caps_a_dominant_category_without_padding_sparse_categories(priority_pool):
    p = priority_pool
    telecom = [
        {
            "key": f"dominant-telecom-{index:03d}", "tier": "T1", "job_score": 80,
            "company": f"通信招聘主体-{index:03d}", "primary_category": TELECOM,
        }
        for index in range(100)
    ]
    banks = [
        {
            "key": f"sparse-bank-{index:02d}", "tier": "T1.5", "job_score": 75,
            "company": f"银行招聘主体-{index:02d}", "primary_category": BANKS,
        }
        for index in range(7)
    ]
    big_four = [
        {
            "key": f"sparse-big-four-{index:02d}", "tier": "T2", "job_score": 70,
            "company": f"四大招聘主体-{index:02d}", "primary_category": BIG_FOUR,
        }
        for index in range(2)
    ]
    p.insert_many(telecom + banks + big_four)

    balanced = p.get(page_size=200, filters={"balanced_only": True})
    assert balanced["stats"]["visible_category_counts"] == {
        TELECOM: 24, BANKS: 7, BIG_FOUR: 2,
    }
    assert balanced["total_opportunities"] == 33
    assert balanced["stats"]["priority_total"] == 109
    # A sparse starfield contributes every real record it has. It is never
    # padded to the category limit, and the complete pool remains reversible.
    assert p.get(page_size=200, filters={"priority_only": True})["total"] == 109


def test_balanced_company_scope_does_not_receive_a_fresh_category_quota(priority_pool):
    p = priority_pool
    p.insert_many([
        {
            "key": f"global-{index:02d}", "tier": "T2", "job_score": 70,
            "company": f"Global Employer {index:02d}", "primary_category": TECH,
        }
        for index in range(61)
    ])
    all_groups = p.get(page_size=100, filters={"view": "companies"})
    excluded = next(item for item in all_groups["items"] if item["company_name"] == "Global Employer 60")
    scoped = p.get(filters={"balanced_only": True, "company_key": excluded["company_key"]})
    assert scoped["total"] == scoped["total_opportunities"] == 0
    assert scoped["stats"]["priority_total"] == 1
    assert scoped["stats"]["balanced_total"] == 0
    assert scoped["stats"]["balanced_excluded_total"] == 1
    assert scoped["stats"]["visible_category_counts"] == {}


def test_api_priority_bool_defaults_false_and_validates_without_real_auth_or_lifespan(priority_pool, monkeypatch):
    from fastapi import Depends, FastAPI
    from fastapi.params import Query
    from fastapi.testclient import TestClient
    from backend import main

    p = priority_pool
    p.insert_many([{"key": "api-priority", "tier": "T2"}, {"key": "api-secondary", "tier": "不建议投"}])
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=p.repository))
    monkeypatch.setattr(main.database, "get_recruitment_profile", lambda _id: {})
    monkeypatch.setattr(main, "_public_reference_url", p.public_url)
    monkeypatch.setattr(main, "_public_radar_opportunity", lambda row, _profile: p.prepare(row))
    monkeypatch.setattr(main, "_radar_company_aliases", lambda: {})
    monkeypatch.setattr(main, "_radar_scoring_scope", lambda _id, _profile: "isolated-priority-api")
    monkeypatch.setattr(main, "_radar_search_metadata", lambda: {})

    original = main.future_radar_opportunities
    signature = inspect.signature(original)
    annotations = get_type_hints(original, include_extras=True)
    parameter = signature.parameters["priority_only"]
    default = parameter.default.default if isinstance(parameter.default, Query) else parameter.default
    assert default is False and annotations["priority_only"] is bool
    balanced_parameter = signature.parameters["balanced_only"]
    balanced_default = (
        balanced_parameter.default.default
        if isinstance(balanced_parameter.default, Query)
        else balanced_parameter.default
    )
    assert balanced_default is False and annotations["balanced_only"] is bool

    def synthetic_user():
        return {"id": 0}

    def endpoint(**kwargs):
        return original(**kwargs)

    # Keep the real query annotations/defaults and handler, replacing only auth.
    # This fresh ASGI app has no application startup or configured DB dependency.
    parameters = [parameter.replace(annotation=dict, default=Depends(synthetic_user)) if name == "user"
                  else parameter.replace(annotation=annotations.get(name, parameter.annotation))
                  for name, parameter in signature.parameters.items()]
    endpoint.__signature__ = signature.replace(parameters=parameters, return_annotation=annotations.get("return", inspect.Signature.empty))
    app = FastAPI()
    app.get("/opportunities")(endpoint)
    with TestClient(app) as client:
        for params, expected in (({}, 2), ({"priority_only": "false"}, 2), ({"priority_only": "true"}, 1)):
            response = client.get("/opportunities", params=params)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["total"] == expected
            assert result["stats"]["priority_total"] == result["stats"]["secondary_total"] == 1
        groups = client.get("/opportunities", params={"priority_only": "true", "view": "companies"}).json()
        assert groups["total_companies"] == groups["total_opportunities"] == 1
        balanced = client.get("/opportunities", params={"balanced_only": "true"}).json()
        assert balanced["total"] == 1 and balanced["stats"]["selection_mode"] == "balanced"
        assert client.get("/opportunities", params={"priority_only": "true", "tier_code": "BELOW_PRIORITY"}).json()["total"] == 0
        assert client.get("/opportunities", params={"priority_only": "not-a-boolean"}).status_code == 422
        assert client.get("/opportunities", params={"balanced_only": "not-a-boolean"}).status_code == 422
