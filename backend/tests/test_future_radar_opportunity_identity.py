"""Display-only ATS identity tests against isolated SQLite and public fixtures.

No accounts, source scans, network calls or production database connections.
"""

import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "unused-identity-tests")
os.environ.setdefault("JWT_SECRET", "isolated-identity-test-secret-32-bytes")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from backend.future_radar.opportunity_cache import install_opportunity_revision
from backend.future_radar.repository import RadarRepository, utc_now
from backend.future_radar.schema import migrate


def workday(req="R-287568", *, tenant="mastercard", site="campus", locale="en-US", slug="Role", apply=False):
    prefix = f"{locale}/" if locale else ""
    suffix = "/apply" if apply else ""
    return f"https://{tenant}.wd1.myworkdayjobs.com/{prefix}{site}/job/Location/{slug}_{req}{suffix}"


def identity(**overrides):
    return RadarRepository._opportunity_identity({
        "external_id": "fixture", "company": "Mastercard", "title": "2027 Graduate Analyst",
        "city": "香港", "official_url": workday(), **overrides,
    }, {})


@pytest.fixture
def pool(tmp_path):
    path = tmp_path / "opportunity-identity.db"

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
        {"id": "history", "name": "公开历史线索", "source_type": "chatgpt_sync", "trust_level": "discovery"},
        {"id": "backfill", "name": "公开招聘补录", "source_type": "other_public_source", "trust_level": "discovery"},
        {"id": "official", "name": "公开官网核验", "source_type": "official_api", "trust_level": "verification"},
    ])
    calls = []

    def insert(key, *, source_id="history", **overrides):
        item = {
            "external_id": key, "company": "Mastercard", "title": "2027 Graduate Analyst",
            "city": "香港", "region": "香港", "employer_type": "金融科技", "industry": "支付科技",
            "primary_category": "foreign_banks_professional_services", "status": "open",
            "verification_status": "pending", "official_url": workday(),
            "requirements": "2027届毕业生，参与数据分析、业务研究与产品支持。",
            "tags": ["校园招聘", "2027届"], "content_hash": key, **overrides,
        }
        with repository.transaction() as connection:
            saved = repository.insert_job(connection, item, source_id=source_id, program_id=None, now=utc_now())
            repository.link_job_source(
                connection, job_id=saved["id"], source=repository.get_source(source_id),
                source_url=item["official_url"], now=utc_now(),
                verification_role="verification" if source_id == "official" else "discovery",
            )
        return saved

    def prepare(row):
        calls.append(row["external_id"])
        fields = ("id", "external_id", "company", "title", "city", "status", "verification_status",
                  "primary_category", "official_url", "application_url", "sources")
        return {key: row.get(key) for key in fields} | {"tier_code": "T2"}

    def public_url(value):
        return value if isinstance(value, str) and value.startswith("https://") else None

    def get(**filters):
        return repository.list_opportunities(
            public_url=public_url, prepare=prepare, cache_scope="isolated-identity-fixture",
            filters=filters or {},
        )

    def detail(key):
        return repository.get_prepared_opportunity(
            key, public_url=public_url, prepare=prepare, cache_scope="isolated-identity-fixture",
        )

    return SimpleNamespace(insert=insert, get=get, detail=detail, calls=calls, repository=repository, connect=connect)


@pytest.mark.parametrize("req, role, city", [
    ("R-287568", "Associate Consultant", "北京"),
    ("R-287570", "Associate Consultant", "香港"),
    ("R-288689", "Associate Analyst, Account Management", "香港"),
    ("R-288691", "Associate Analyst, Business Development", "香港"),
])
def test_same_public_workday_requisition_merges_alias_titles_cities_and_sources(pool, req, role, city):
    original = pool.insert(
        "original", source_id="official", company="万事达卡（Mastercard）",
        title=f"{role}, Launch Graduate Program 2027", city=city,
        official_url=workday(req, locale="zh-CN", slug="Original-Title"), verification_status="verified",
    )
    discovery = pool.insert(
        "history-copy", title=f"2027 Graduate – {role}", city="待岗位页确认",
        official_url=workday(req.lower(), locale="", site="Campus", slug="Different-Title") + "?utm_source=history",
        application_url=workday(req, locale="en-US", apply=True), status="unknown",
    )
    result = pool.get()
    assert result["total"] == 1
    winner = result["items"][0]
    assert winner["id"] == original["id"]
    assert winner["company"] == "万事达卡（Mastercard）"
    assert winner["verification_status"] == "verified"
    assert {source["source_id"] for source in winner["sources"]} == {"history", "official"}
    for alias in (original["id"], original["external_id"], discovery["id"], discovery["external_id"]):
        assert pool.detail(alias)["id"] == original["id"]
    assert pool.get()["total"] == 1
    assert pool.calls == [original["external_id"]]
    with pool.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 2
        stored = connection.execute("SELECT status,verification_status FROM radar_jobs WHERE id=?", (discovery["id"],)).fetchone()
    assert tuple(stored) == ("unknown", "pending")


def test_same_title_city_but_different_requisitions_do_not_merge(pool):
    for req in ("R-287568", "R-287570"):
        pool.insert(req, official_url=workday(req))
    # A weak campaign-only row must not bridge two strong ATS identities.
    pool.insert("without-req", official_url="https://mastercard.wd1.myworkdayjobs.com/en-US/campus")
    assert pool.get()["total"] == 3


def test_requisition_scope_keeps_employers_tenants_and_career_sites_separate(pool):
    pool.insert("root")
    pool.insert("other-company", company="其他招聘公司")
    pool.insert("partner", company="万事达卡合作伙伴（Mastercard）")
    pool.insert("branch", company="万事达卡中国分公司（Mastercard）")
    pool.insert("other-tenant", official_url=workday(tenant="another-employer"))
    pool.insert("other-board", official_url=workday(site="Experienced"))
    assert pool.get()["total"] == 6


def test_conflicting_official_and_application_requisitions_stay_isolated(pool):
    pool.insert("first", official_url=workday("R-287568"))
    pool.insert("second", official_url=workday("R-287570"))
    pool.insert("conflicting", official_url=workday("R-287568"), application_url=workday("R-287570"))
    pool.insert("other-conflicting", official_url=workday("R-287568"), application_url=workday("R-287570"))
    assert pool.get()["total"] == 4


@pytest.mark.parametrize("url", [
    "https://mastercard.wd1.myworkdayjobs.com/en-US/campus?jobId=287568",
    "https://mastercard.wd1.myworkdayjobs.com/en-US/campus/job?req=R-287568",
    "https://mastercard.wd1.myworkdayjobs.com/en-US/campus/job/Graduate-Programme-2027",
    "https://mastercard.wd1.myworkdayjobs.com.evil.invalid/en-US/campus/job/Role_R-287568",
    "https://mastercard.wd1.myworkdayjobs.com@evil.invalid/en-US/campus/job/Role_R-287568",
    "https://user:password@mastercard.wd1.myworkdayjobs.com/en-US/campus/job/Role_R-287568",
    "https://@mastercard.wd1.myworkdayjobs.com/en-US/campus/job/Role_R-287568",
    "https://mastercard.wd1.myworkdayjobs.com:80/en-US/campus/job/Role_R-287568",
    "https://notmyworkdayjobs.com/en-US/campus/job/Role_R-287568",
    "https://careers.example.invalid/job/Role_R-287568",
    "https://app.mokahr.com/campus-recruitment/company/287568#/jobs",
    "https://career.cmbchina.com/positionlist/287568?orgId=123",
])
def test_campaign_ids_and_spoofed_hosts_are_not_independent_workday_references(url):
    assert RadarRepository._workday_opportunity_reference(url) is None


def test_workday_requisition_prefix_and_leading_zeroes_are_preserved():
    assert identity(official_url=workday("R-012345")) != identity(official_url=workday("R-12345"))
    assert identity(official_url=workday("JR-012345")) != identity(official_url=workday("R-012345"))


def test_broad_brand_alias_mapping_does_not_erase_an_explicit_hiring_unit():
    aliases = {"万事达卡中国分公司mastercard": "mastercard"}
    branch = {"company": "万事达卡中国分公司（Mastercard）", "official_url": workday()}
    root = {"company": "Mastercard", "official_url": workday()}
    assert RadarRepository._opportunity_identity(branch, aliases) != RadarRepository._opportunity_identity(root, aliases)


def test_shared_generic_campaign_url_keeps_roles_hiring_units_and_cohorts(pool):
    url = "https://careers.example.invalid/campus/2027"
    pool.insert("analyst", title="2027校园招聘数据分析岗", official_url=url)
    pool.insert("product", title="2027校园招聘产品经理", official_url=url)
    pool.insert("other-year", title="2028校园招聘数据分析岗", official_url=url, tags=["2028届"])
    pool.insert("other-unit", company="示例科技北京分公司", title="2027校园招聘数据分析岗", official_url=url)
    assert pool.get()["total"] == 4


def test_explicitly_different_cohorts_with_same_workday_reference_stay_separate(pool):
    pool.insert("2027", title="2027 Graduate Analyst")
    pool.insert("2028", title="2028 Graduate Analyst", tags=["2028届"])
    assert pool.get()["total"] == 2


def test_kpmg_advisory_only_and_full_program_are_not_the_same_opportunity(pool):
    url = "https://kpmg.com/cn/zh/careers/campus/graduate-applications.html"
    pool.insert(
        "kpmg-advisory", source_id="backfill", company="毕马威中国",
        title="2027届校园招聘项目", city="待岗位页确认", official_url=url,
        requirements="仅限毕马威2027应届生咨询（Advisory）招聘范围，不扩展到审计或税务项目。",
    )
    pool.insert(
        "kpmg-full", company="毕马威中国", title="2027秋季校园招聘/应届生项目",
        city="中国大陆及香港", official_url=url,
        requirements="2027年学士及以上应届生；岗位覆盖审计、税务和咨询。",
    )
    pool.insert(
        "kpmg-next-year", company="毕马威中国", title="2028秋季校园招聘/应届生项目",
        city="中国大陆及香港", official_url=url, tags=["2028届"],
    )
    pool.insert(
        "kpmg-singapore", company="KPMG Singapore", title="2027 Graduate Associate – Risk & Regulatory Advisory",
        city="新加坡", official_url="https://careers.kpmg.com.sg/job/Risk-Regulatory-Advisory-Graduate-Associate-2027/58740544/",
    )
    pool.insert(
        "kpmg-singapore-ba", company="KPMG Singapore", title="2027 Graduate Associate – Business Analyst",
        city="新加坡", official_url="https://careers.kpmg.com.sg/job/Customer-Transformation-Graduate-Associate-2027/58842844/",
    )
    assert pool.get()["total"] == 5


def test_verified_closed_requisition_cannot_be_resurrected_by_filters_or_aliases(pool):
    official = pool.insert(
        "closed-original", source_id="official", company="万事达卡（Mastercard）",
        verification_status="verified", status="closed",
    )
    stale = pool.insert(
        "stale-open", city="待岗位页确认", title="2027 Campus Associate",
        official_url=workday(locale="", slug="Alternative-Title"),
    )
    assert pool.get()["total"] == 0
    assert pool.get(source_id="history")["total"] == 0
    assert pool.get(verification_status="pending")["total"] == 0
    archived = pool.get(status="closed", source_id="history")
    assert archived["total"] == 1
    assert archived["items"][0]["id"] == official["id"]
    assert {s["source_id"] for s in archived["items"][0]["sources"]} == {"official", "history"}
    assert pool.detail(stale["id"])["id"] == official["id"]
    assert pool.repository.get_job(stale["id"])["status"] == "open"


def test_company_view_and_full_pool_counts_use_one_winner_per_requisition(pool):
    for number in range(7):
        req = f"R-88000{number}"
        pool.insert(f"official-{number}", source_id="official", verification_status="verified", official_url=workday(req))
        pool.insert(f"history-{number}", source_id="history", title="2027 Another Graduate Title", official_url=workday(req, locale=""))
    jobs = pool.get()
    groups = pool.get(view="companies")
    assert jobs["total"] == 7
    assert groups["total_opportunities"] == 7
    assert groups["total_companies"] == 1
    assert groups["items"][0]["opportunity_count"] == 7
    assert len(pool.calls) == 7
