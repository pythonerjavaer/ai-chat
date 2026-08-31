"""Full-pool display grouping using only isolated SQLite and public fixtures.

No accounts, network, source runs, production databases or scoring changes.
"""

import os
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "not-used-in-company-group-tests")
os.environ.setdefault("JWT_SECRET", "isolated-company-group-test-secret-32-bytes")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")

from backend.future_radar.normalization import normalized_key
from backend.future_radar.opportunity_cache import install_opportunity_revision
from backend.future_radar.repository import RadarRepository, utc_now
from backend.future_radar.schema import migrate


@pytest.fixture
def pool(tmp_path):
    path = tmp_path / "company-display.db"

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    with connect() as connection:
        migrate(connection)
        connection.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
        install_opportunity_revision(connection)
    repository = RadarRepository(connect)
    repository.seed_sources([
        {"id": "public-fixture", "name": "公开招聘线索", "source_type": "chatgpt_sync", "trust_level": "discovery"},
        {"id": "other-fixture", "name": "公开官网记录", "source_type": "official_api", "trust_level": "official"},
    ])
    calls = []
    scores = {}

    def insert(key, *, tier="T2", source_id="public-fixture", **overrides):
        item = {
            "external_id": key, "company": "示例科技", "title": f"2027校园招聘数据分析岗 {key}",
            "city": "上海", "region": "中国大陆", "employer_type": "科技企业", "industry": "科技",
            "primary_category": "internet_tech", "verification_status": "pending", "status": "open",
            "description": "面向应届毕业生的数据分析和产品岗位。", "requirements": "2027届毕业生。",
            "official_url": f"https://careers.example.invalid/campus/{key}",
            "tags": ["校园招聘", "2027届"], "content_hash": key, **overrides,
        }
        with repository.transaction() as connection:
            saved = repository.insert_job(connection, item, source_id=source_id, program_id=None, now=utc_now())
            repository.link_job_source(
                connection, job_id=saved["id"], source=repository.get_source(source_id),
                source_url=item["official_url"], now=utc_now(),
                verification_role="discovery" if source_id == "public-fixture" else "verification",
            )
        scores[key] = tier
        return saved

    def prepare(row):
        calls.append((row["external_id"], row["company"]))
        public_fields = (
            "id", "external_id", "company", "title", "city", "primary_category", "status",
            "verification_status", "official_url", "closing_date", "sources",
        )
        return {key: row.get(key) for key in public_fields} | {
            "tier_code": scores[row["external_id"]], "listing_kind": "job",
        }

    def public_url(value):
        return value if isinstance(value, str) and value.startswith("https://careers.example.invalid/") else None

    def get(*, filters=None, **kwargs):
        return repository.list_opportunities(
            public_url=public_url, prepare=prepare, cache_scope="isolated-public-fixture",
            filters={"status": "all", **(filters or {})}, **kwargs,
        )

    return SimpleNamespace(insert=insert, get=get, calls=calls, repository=repository, prepare=prepare, public_url=public_url)


def test_company_pagination_uses_the_entire_pool_not_the_first_job_page(pool):
    for index in range(75):
        pool.insert(f"carrier-{index}", company="中国联合网络通信集团有限公司", primary_category="state_tech_telecom")
    for index in range(25):
        pool.insert(f"other-{index}", company=f"Employer {index:02d}")
    first = pool.get(page_size=20, filters={"view": "companies"})
    second = pool.get(page=2, page_size=20, filters={"view": "companies"})
    groups = first["items"] + second["items"]
    assert first["view"] == "companies"
    assert first["total"] == first["total_companies"] == 26
    assert first["total_opportunities"] == first["stats"]["total_opportunities"] == 100
    assert len(groups) == len({group["company_key"] for group in groups}) == 26
    assert sum(group["opportunity_count"] for group in groups) == 100
    carrier = next(group for group in groups if group["company_name"] == "中国联通")
    assert carrier["opportunity_count"] == 75
    assert carrier["grouping"] == "telecom_group"
    one = pool.get(page_size=50, filters={"company_key": carrier["company_key"]})
    two = pool.get(page=2, page_size=50, filters={"company_key": carrier["company_key"]})
    assert one["view"] == "jobs"
    assert one["total"] == 75 and two["total"] == 75
    assert len(one["items"]) == 50 and len(two["items"]) == 25
    assert len({item["id"] for item in one["items"] + two["items"]}) == 75
    assert all(item["company"] == "中国联合网络通信集团有限公司" for item in one["items"] + two["items"])


def test_default_jobs_and_groups_keep_all_tiers_and_scored_actual_entities(pool):
    examples = [
        ("中国电信集团总部", "T1"), ("中国电信安徽省分公司", "T2"),
        ("中国电信石家庄市分公司", "T2.5"), ("中国电信庐江县分公司", "T3"),
        ("天翼云科技有限公司", "T1.5"), ("中国电信合作伙伴", "不建议投"),
    ]
    for index, (company, tier) in enumerate(examples):
        pool.insert(f"entity-{index}", company=company, tier=tier)
    jobs = pool.get()
    assert jobs["view"] == "jobs" and jobs["total"] == len(examples)
    assert {(job["company"], job["tier_code"]) for job in jobs["items"]} == set(examples)
    groups = pool.get(filters={"view": "companies"})
    assert groups["total_companies"] == 2
    assert sorted(group["opportunity_count"] for group in groups["items"]) == [1, 5]
    assert set(pool.calls) == {(f"entity-{index}", company) for index, (company, _tier) in enumerate(examples)}
    assert len(pool.calls) == len(examples), "view switching must not rerun full-pool scoring"


def test_tier_category_query_and_company_scope_are_applied_before_grouping(pool):
    for index in range(61):
        pool.insert(f"routine-{index}", company="中国电信", tier="T2", primary_category="state_tech_telecom")
    pool.insert("rare-top", company="中国电信安徽省分公司", tier="T0.5", primary_category="state_tech_telecom")
    pool.insert("same-brand-other-category", company="中国电信", tier="T0.5", primary_category="internet_tech")
    pool.insert("another-company-top", company="其他科技", tier="T0.5", primary_category="state_tech_telecom")
    filters = {"view": "companies", "tier_code": "T0.5", "primary_categories": ["state_tech_telecom"]}
    result = pool.get(filters=filters)
    assert result["total_companies"] == result["total_opportunities"] == 2
    carrier = next(group for group in result["items"] if group["company_name"] == "中国电信")
    assert carrier["opportunity_count"] == 1 and carrier["tier_counts"] == {"T0.5": 1}
    expanded = pool.get(filters={**filters, "view": "jobs", "company_key": carrier["company_key"]})
    assert [job["external_id"] for job in expanded["items"]] == ["rare-top"]
    assert expanded["stats"]["tier_counts"]["T2"] == 61
    assert expanded["total_opportunities"] == expanded["total_companies"] == 1
    searched = pool.get(filters={"view": "companies", "q": "rare-top"})
    assert searched["total_opportunities"] == 1


def test_company_expand_retains_status_city_source_and_search_filters(pool):
    pool.insert("wanted", company="中国联通", city="上海", verification_status="verified", source_id="other-fixture")
    pool.insert("closed", company="中国联通", city="上海", status="closed", verification_status="verified", source_id="other-fixture")
    pool.insert("other-city", company="中国联通", city="北京", verification_status="verified", source_id="other-fixture")
    pool.insert("other-source", company="中国联通", city="上海")
    filters = {"view": "companies", "status": "open", "city": "上海", "source_id": "other-fixture"}
    result = pool.get(filters=filters)
    key = result["items"][0]["company_key"]
    jobs = pool.get(filters={**filters, "view": "jobs", "company_key": key})
    assert result["total_opportunities"] == jobs["total"] == 1
    assert jobs["items"][0]["external_id"] == "wanted"
    assert pool.get(filters={"company_key": "company:does-not-exist"})["total"] == 0


def test_unknown_employers_are_not_collapsed_into_one_company(pool):
    for index, name in enumerate(["未知公司", "未知公司", "未披露", "unknown", "招聘单位待确认"]):
        pool.insert(f"unknown-{index}", company=name)
    result = pool.get(filters={"view": "companies"})
    assert result["total"] == result["total_opportunities"] == 5
    assert len({group["company_key"] for group in result["items"]}) == 5
    assert all(group["grouping"] == "unknown" for group in result["items"])
    again = pool.get(filters={"view": "companies"})
    assert again["items"] == result["items"]
    assert pool.repository._opportunity_display_company({"id": "one", "company": ""}, {})[0] != \
        pool.repository._opportunity_display_company({"id": "two", "company": ""}, {})[0]


def test_company_aliases_merge_for_display_but_not_unrelated_partners(pool):
    pool.insert("cn", company="腾讯")
    pool.insert("en", company="Tencent")
    pool.insert("partner", company="腾讯云合作伙伴")
    aliases = {normalized_key(name): "腾讯" for name in ["腾讯", "Tencent"]}
    result = pool.get(filters={"view": "companies"}, company_aliases=aliases)
    assert result["total"] == 2 and result["total_opportunities"] == 3
    assert sorted(group["opportunity_count"] for group in result["items"]) == [1, 2]
    assert any(group["company_name"] == "腾讯云合作伙伴" for group in result["items"])


def test_company_sort_is_name_stable_and_not_changed_time_or_volume(pool):
    pool.insert("z-one", company="Zeta")
    pool.insert("a-one", company="Alpha")
    first = pool.get(page_size=1, filters={"view": "companies", "sort": "changed"})
    assert first["items"][0]["company_name"] == "Alpha"
    for index in range(12):
        pool.insert(f"z-new-{index}", company="Zeta")
    second = pool.get(page_size=1, filters={"view": "companies", "sort": "changed"})
    assert second["items"][0]["company_key"] == first["items"][0]["company_key"]
    assert second["company_sort"] == "name"


def test_groups_and_expansion_share_cache_without_exposing_mutable_values(pool):
    for index in range(6):
        pool.insert(f"role-{index}", company="中国移动", tier="T1" if index == 5 else "T2")
    groups = pool.get(filters={"view": "companies"})
    key = groups["items"][0]["company_key"]
    groups["items"][0]["tier_counts"]["T1"] = 900
    jobs = pool.get(filters={"company_key": key, "tier_code": "T1"})
    assert jobs["total"] == 1
    jobs["items"][0]["company"] = "mutated response only"
    pool.get(page_size=2)
    pool.get(page=2, page_size=2)
    again = pool.get(filters={"view": "companies", "tier_code": "T1"})
    assert again["items"][0]["tier_counts"] == {"T1": 1}
    assert again["items"][0]["hiring_units"] == ["中国移动"]
    assert len(pool.calls) == 6


def test_company_deadline_preview_uses_matching_pool_not_company_page(pool):
    future = (date.today() + timedelta(days=3)).isoformat()
    pool.insert("first", company="Alpha", tier="T2")
    pool.insert("deadline", company="Zeta", tier="T1", closing_date=future, verification_status="verified")
    result = pool.get(page_size=1, filters={"view": "companies"})
    assert result["items"][0]["company_name"] == "Alpha"
    assert [item["external_id"] for item in result["deadline_opportunities"]] == ["deadline"]
    assert pool.get(filters={"view": "companies", "tier_code": "T2"})["deadline_opportunities"] == []


def test_api_defaults_to_jobs_and_company_aliases_do_not_masquerade_as_job_lists(pool, monkeypatch):
    # Exercise response assembly directly: no HTTP auth/account registration,
    # lifespan hooks or access to any configured application database.
    import inspect
    import json
    from fastapi.params import Query
    from backend import main

    pool.insert("api-one", company="中国联通")
    pool.insert("api-two", company="中国联合网络通信有限公司安徽省分公司")
    monkeypatch.setattr(main, "future_radar_service", SimpleNamespace(repository=pool.repository))
    monkeypatch.setattr(main.database, "get_recruitment_profile", lambda _id: {})
    monkeypatch.setattr(main, "_public_reference_url", pool.public_url)
    monkeypatch.setattr(main, "_public_radar_opportunity", lambda row, _profile: pool.prepare(row))
    monkeypatch.setattr(main, "_radar_company_aliases", lambda: {})
    monkeypatch.setattr(main, "_radar_scoring_scope", lambda _id, _profile: "isolated-public-fixture")
    monkeypatch.setattr(main, "_radar_search_metadata", lambda: {})
    signature = inspect.signature(main.future_radar_opportunities)
    kwargs = {name: parameter.default.default if isinstance(parameter.default, Query) else parameter.default
              for name, parameter in signature.parameters.items() if name != "user"}
    kwargs["user"] = {"id": 0}
    assert kwargs["view"] == "jobs"
    jobs = json.loads(main.future_radar_opportunities(**kwargs).body)
    assert jobs["view"] == "jobs" and jobs["total"] == 2
    assert jobs["items"] == jobs["jobs"] == jobs["opportunities"]
    groups = json.loads(main.future_radar_opportunities(**{**kwargs, "view": "companies"}).body)
    assert groups["total"] == groups["total_companies"] == 1
    assert groups["total_opportunities"] == 2
    assert groups["companies"] == groups["items"]
    assert "jobs" not in groups and "opportunities" not in groups
    compact = json.loads(main.future_radar_opportunities(**{**kwargs, "view": "companies", "compact": True}).body)
    assert compact["items"] == groups["items"] and "companies" not in compact
    expanded = json.loads(main.future_radar_opportunities(**{
        **kwargs, "company_key": groups["items"][0]["company_key"], "tier_code": "T2",
    }).body)
    assert expanded["view"] == "jobs" and expanded["total"] == 2
