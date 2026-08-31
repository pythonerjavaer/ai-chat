"""Pure employer classification and isolated SQLite/Postgres backfill checks.

No model calls, website fetches, accounts, production credentials or scans.
"""

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "category-test-unused-no-network")
os.environ.setdefault("JWT_SECRET", "category-unit-test-synthetic-secret-32")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("RECRUITMENT_WEB_SEARCH_ENABLED", "false")

from backend.future_radar.normalization import SEMANTIC_JOB_FIELDS, normalize_job, semantic_hash
from backend.future_radar.opportunity_cache import install_opportunity_revision, read_opportunity_revision
from backend.future_radar.repository import RadarRepository, utc_now
from backend.future_radar.schema import EMPLOYER_CATEGORY_MIGRATION, migrate
from backend.recruitment import SCORING_VERSION, SCORING_WEIGHTS, score_job
from backend.recruitment_directory import employer_category_override, employer_directory_category


@pytest.mark.parametrize("company,employer_type,industry,expected", [
    ("杭州银行", "商业银行", "银行", "policy_state_banks"),
    ("中信银行股份有限公司", "股份制商业银行总行", "银行/FinTech/管培", "policy_state_banks"),
    ("中信银行信用卡中心", "中信银行总行信用卡专营机构", "消费金融/数据", "policy_state_banks"),
    ("杭银理财", "银行理财子公司", "金融科技", "securities_public_funds_asset_management"),
    ("光大证券", "证券公司", "人工智能/咨询", "securities_public_funds_asset_management"),
    ("示例公募机构", "公募基金管理公司", "科技/咨询", "securities_public_funds_asset_management"),
    ("示例私募机构", "私募证券基金管理人", "资产管理", "quant_private_hedge"),
    ("KPMG Australia", "重点雇主", "咨询/AI", "big_four_professional_services"),
    ("PwC 普华永道", "外资专业服务机构", "咨询", "big_four_professional_services"),
    ("Deloitte", "其他", "其他", "big_four_professional_services"),
    ("McKinsey 麦肯锡", "重点雇主", "", "consumer_foreign_consulting"),
    ("Accenture 埃森哲", "外资专业服务机构", "专业服务", "consumer_foreign_consulting"),
    ("万事达卡（Mastercard）", "外资企业", "支付科技/客户管理", "consumer_foreign_consulting"),
    ("Goldman Sachs 高盛", "外资投资银行", "全球市场/量化策略", "consumer_foreign_consulting"),
    ("HSBC 汇丰", "外资银行", "金融科技", "consumer_foreign_consulting"),
    ("BlackRock 贝莱德", "重点雇主", "", "consumer_foreign_consulting"),
    ("BNP Paribas 法国巴黎银行", "外资银行", "全球市场", "policy_state_banks"),
    ("平安银行大连分行", "重点雇主", "", "insurance_integrated_finance"),
    ("中国人民保险集团股份有限公司", "重点雇主", "", "insurance_integrated_finance"),
    ("中国平安保险(集团)股份有限公司", "重点雇主", "", "insurance_integrated_finance"),
    ("腾讯科技有限公司", "重点雇主", "", "internet_tech"),
    ("TCL", "大型科技制造集团", "智能终端", "internet_tech"),
    ("中国能源建设集团有限公司", "中央企业集团", "工程建设", "state_energy_resources"),
    ("中国电子科技集团公司第十四研究所", "重点雇主", "", "state_tech_telecom"),
    ("中国航天科工二院二十三所", "重点雇主", "", "state_tech_telecom"),
    ("中国电信镇江分公司本部", "重点雇主", "", "state_tech_telecom"),
])
def test_real_employer_identity_or_explicit_type_wins_over_role_keywords(
    company, employer_type, industry, expected,
):
    raw = {"company": company, "title": "2027校园招聘AI/量化/咨询分析岗",
           "employer_type": employer_type, "industry": industry,
           "requirements": "职责可以涉及其他行业，不代表雇主行业。"}
    assert normalize_job(raw)["primary_category"] == expected
    assert employer_category_override(raw) == expected


@pytest.mark.parametrize("company", [
    "Deloitte招聘合作伙伴", "某公司（腾讯供应商）", "腾讯客户服务商",
    "为高盛提供咨询服务", "不是腾讯科技", "Google UBS", "星河产品实验室",
    "腾讯未来未知子公司有限公司", "中国能源建设集团浙江火电建设有限公司",
])
def test_unknown_affiliates_and_names_in_prose_are_not_promoted(company):
    item = normalize_job({"company": company, "title": "腾讯客户AI咨询项目分析师",
                          "requirements": "为德勤客户开展人工智能与金融咨询。",
                          "employer_type": "重点雇主", "industry": ""})
    assert employer_directory_category(company) == ""
    assert item["primary_category"] == ""


def test_directory_does_not_rewrite_an_unrecognized_explicit_category():
    raw = {"company": "未知企业", "title": "分析师", "primary_category": "internet_tech"}
    assert normalize_job(raw)["primary_category"] == "internet_tech"


@pytest.mark.parametrize("metadata,expected", [
    ({"primary_category": "quant_private_hedge", "employer_type": "券商/公募/资管"}, "quant_private_hedge"),
    ({"primary_category": "internet_tech", "employer_type": "银行/金融"}, "internet_tech"),
    ({"organization_category": "public_fund", "employer_type": "银行/金融"}, "securities_public_funds_asset_management"),
])
def test_generic_employer_type_does_not_override_explicit_organization(metadata, expected):
    raw = {"company": "未列入目录的机构", "title": "数据分析师", **metadata}
    assert normalize_job(raw)["primary_category"] == expected


def test_offline_classifier_has_no_application_configuration_dependency():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from backend.future_radar.normalization import normalize_job; "
         "assert normalize_job({'company':'KPMG','title':'分析师'})['primary_category']=='big_four_professional_services'; "
         "assert not {'backend.config','backend.database','backend.main','openai','psycopg'}.intersection(sys.modules)"],
        cwd=Path(__file__).resolve().parents[2],
        env={"PATH": "", "PYTHON_DOTENV_DISABLED": "1", "PYTHONNOUSERSITE": "1"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_existing_search_and_sidebar_exports_use_one_shared_directory():
    from backend import live_sources, recruitment_directory, recruitment_search
    assert live_sources.PERSONAL_MONITOR_POOLS is recruitment_directory.PERSONAL_MONITOR_POOLS
    assert recruitment_search.PERSONAL_MONITOR_POOLS is recruitment_directory.PERSONAL_MONITOR_POOLS
    assert recruitment_search.EMPLOYER_ALIAS_GROUPS is recruitment_directory.EMPLOYER_ALIAS_GROUPS
    bank_pool = next(pool for pool in live_sources.PERSONAL_MONITOR_POOLS if pool["primary_category"] == "policy_state_banks")
    assert bank_pool["name"] == "银行与政策性金融"
    assert len(bank_pool["employers"]) == 10


@pytest.fixture(params=["sqlite", "postgres"])
def isolated_pool(request, tmp_path):
    if request.param == "sqlite":
        path = tmp_path / "categories.db"

        def connect():
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection

        cleanup = lambda: None
    else:
        dsn = os.environ.get("FROSTFIRE_TEST_POSTGRES_URL")
        if not dsn:
            pytest.skip("requires isolated local PostgreSQL test URL")
        assert urlsplit(dsn).hostname in {"127.0.0.1", "localhost", "::1"}, "refuse nonlocal test DB"
        from backend.storage import close_postgres_pools, connect_postgres
        schema = "ff_category_test_" + uuid.uuid4().hex

        def connect():
            return connect_postgres(dsn, schema=schema)

        def cleanup():
            with connect() as connection:
                connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            close_postgres_pools()

    try:
        with connect() as connection:
            migrate(connection)
            connection.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
            install_opportunity_revision(connection)
        repo = RadarRepository(connect)
        repo.seed_sources([{"id": "category-public-fixture", "name": "公开招聘线索",
                            "source_type": "chatgpt_sync", "trust_level": "discovery"}])
        source = repo.get_source("category-public-fixture")

        def insert(key, company, employer_type="", primary_category="", **extra):
            item = {"external_id": key, "company": company, "title": f"2027校园招聘数据分析岗 {key}",
                    "city": "上海", "employer_type": employer_type, "industry": "",
                    "primary_category": primary_category, "verification_status": "pending", "status": "open",
                    "requirements": "负责分析数据、构建业务指标；面向2027届应届毕业生。",
                    "tags": ["校园招聘"], "official_url": f"https://careers.example.invalid/{key}",
                    "content_hash": f"old-hash-{key}", **extra}
            with repo.transaction() as connection:
                saved = repo.insert_job(connection, item, source_id=source["id"], program_id=None, now=utc_now())
                repo.link_job_source(connection, job_id=saved["id"], source=source,
                                     source_url=item["official_url"], now=utc_now(), verification_role="discovery")
            return saved

        def migrate_existing():
            with connect() as connection:
                connection.execute("DELETE FROM schema_migrations WHERE version=?", (EMPLOYER_CATEGORY_MIGRATION,))
                migrate(connection)

        yield SimpleNamespace(connect=connect, repo=repo, insert=insert, migrate_existing=migrate_existing)
    finally:
        cleanup()


def snapshot(pool, table):
    with pool.connect() as connection:
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
        return {row.get("id", (row.get("job_id"), row.get("source_id"))): row for row in rows}


def test_existing_rows_backfill_only_category_and_hash_once(isolated_pool):
    pool = isolated_pool
    bank = pool.insert("bank", "杭州银行", "商业银行")
    four = pool.insert("four", "KPMG", "重点雇主", "consumer_foreign_consulting")
    unknown = pool.insert("unknown", "星河实验室", "重点雇主")
    correct = pool.insert("already-correct", "腾讯", "科技企业", "internet_tech")
    closed = pool.insert("closed", "中信证券", "证券公司", status="closed", verification_status="verified")
    before = {table: snapshot(pool, table) for table in ("radar_jobs", "radar_companies", "monitor_sources", "job_sources", "radar_events")}
    with pool.connect() as connection:
        revision_before = read_opportunity_revision(connection)
    pool.migrate_existing()
    after = {table: snapshot(pool, table) for table in before}
    assert {t: len(rows) for t, rows in before.items()} == {t: len(rows) for t, rows in after.items()}
    assert {t: rows for t, rows in before.items() if t != "radar_jobs"} == {t: rows for t, rows in after.items() if t != "radar_jobs"}
    for saved, expected in [(bank, "policy_state_banks"), (four, "big_four_professional_services"),
                            (closed, "securities_public_funds_asset_management")]:
        old, new = before["radar_jobs"][saved["id"]], after["radar_jobs"][saved["id"]]
        assert {key for key in old if old[key] != new[key]} == {"primary_category", "content_hash"}
        assert new["primary_category"] == expected
        decoded = {**new, **{key: json.loads(new[key]) for key in ("tags", "industry_tags", "role_tags")}}
        assert new["content_hash"] == semantic_hash(decoded, SEMANTIC_JOB_FIELDS)
    for saved in (unknown, correct):
        assert before["radar_jobs"][saved["id"]] == after["radar_jobs"][saved["id"]]
    with pool.connect() as connection:
        assert read_opportunity_revision(connection) != revision_before
        migrate(connection)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=?", (EMPLOYER_CATEGORY_MIGRATION,)).fetchone()[0] == 1
    assert snapshot(pool, "radar_jobs") == after["radar_jobs"]


def test_company_and_job_views_use_backfilled_real_categories_and_invalidate_cache(isolated_pool):
    pool = isolated_pool
    expected = {pool.insert(f"bank-{index}", "杭州银行", "商业银行")["id"] for index in range(3)}
    pool.insert("consulting", "KPMG", "重点雇主", "consumer_foreign_consulting")
    pool.insert("closed-bank", "杭州银行", "商业银行", status="closed")
    pool.insert("unknown", "星河实验室", "重点雇主")
    peer = RadarRepository(pool.connect)

    def public_url(value):
        return value if isinstance(value, str) and value.startswith("https://careers.example.invalid/") else None

    def prepare(row):
        return score_job(row, {})

    def get(repo, *, view="jobs", page=1, category="policy_state_banks"):
        return repo.list_opportunities(page=page, page_size=2, filters={"view": view, "status": "active", "primary_categories": [category]},
                                       public_url=public_url, prepare=prepare, cache_scope="isolated-category-fixture")

    assert get(pool.repo)["total"] == get(peer)["total"] == 0
    pool.migrate_existing()
    first, second = get(peer), get(pool.repo, page=2)
    assert first["total"] == second["total"] == 3
    assert {row["id"] for row in first["items"] + second["items"]} == expected
    grouped = get(peer, view="companies")
    assert grouped["total"] == grouped["total_companies"] == 1
    assert grouped["total_opportunities"] == grouped["items"][0]["opportunity_count"] == 3
    assert grouped["stats"]["category_counts"] == {"policy_state_banks": 3}
    assert get(pool.repo, category="big_four_professional_services")["total"] == 1
    assert get(pool.repo, category="consumer_foreign_consulting")["total"] == 0


@pytest.mark.parametrize("company,kind,old_category", [
    ("中国工商银行股份有限公司上海分行", "国有大行", "policy_state_banks"),
    ("Deloitte", "重点雇主", ""),
    ("Goldman Sachs 高盛", "外资投资银行", "quant_private_hedge"),
    ("平安银行大连分行", "重点雇主", ""),
    ("杭银理财有限责任公司", "银行理财子公司", ""),
    ("腾讯未来未知子公司有限公司", "重点雇主", ""),
    ("某公司（Deloitte合作伙伴）", "重点雇主", ""),
])
def test_navigation_classification_never_changes_employer_hierarchy_or_scoring_rules(company, kind, old_category):
    old = {"company": company, "employer_type": kind, "industry": "", "primary_category": old_category,
           "title": "2027校园招聘数据分析岗", "city": "上海",
           "requirements": "负责分析业务数据、构建数据产品、支持风险管理。", "tags": ["校园招聘"]}
    updated = {**old, "primary_category": normalize_job(old)["primary_category"]}
    before, after = score_job(old, {}), score_job(updated, {})
    assert before["company"] == after["company"] == company
    for key in ("level", "is_group_headquarters", "confidence"):
        assert before["organization_assessment"][key] == after["organization_assessment"][key]
    assert before["scoring_version"] == after["scoring_version"] == SCORING_VERSION
    assert {key: value["weight"] for key, value in after["scoring_factors"].items()} == SCORING_WEIGHTS
    if old_category == updated["primary_category"]:
        for key in ("tier_code", "job_score", "score_breakdown", "organization_assessment"):
            assert before[key] == after[key]
