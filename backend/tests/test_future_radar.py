import asyncio
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-tests")
os.environ.setdefault("RECRUITMENT_INGEST_TOKEN", "test-recruitment-ingest-token")
os.environ.setdefault("ADMIN_DASHBOARD_TOKEN", "test-admin-dashboard-token")
os.environ.setdefault("RECRUITMENT_REFRESH_MINUTES", "0")
os.environ.setdefault("FUTURE_RADAR_ENABLED", "false")

from fastapi.testclient import TestClient

from backend import database, main
from backend.future_radar.adapters import (
    AdapterResult,
    DiscoveryLimitedError,
    LegacyDatabaseAdapter,
    OfficialHtmlAdapter,
    OpenAIWebSearchAdapter,
    PublicFeedAdapter,
    WechatSourceAdapter,
    WechatWebSearchAdapter,
    _public_reference_url,
    adapter_for_source,
)
from backend.future_radar.normalization import (
    PRIMARY_CATEGORY_CODES,
    normalize_job,
)
from backend.future_radar.schema import migrate
from backend.future_radar.schemas import FrostFireSyncV1, RadarJobInput, SourceCreateRequest
from backend.future_radar.service import FutureRadarService, SyncConflict
from backend.recruitment_watch import normalize_html_text


class StaticAdapter:
    def __init__(self, result: AdapterResult):
        self.result = result

    def scan(self, source):
        del source
        return deepcopy(self.result)


class SequenceAdapter:
    def __init__(self, results: list[AdapterResult]):
        self.results = list(results)

    def scan(self, source):
        del source
        if not self.results:
            raise AssertionError("SequenceAdapter was scanned more times than expected")
        return deepcopy(self.results.pop(0))


class FailingAdapter:
    def __init__(self, message: str = "adapter failed"):
        self.message = message

    def scan(self, source):
        del source
        raise RuntimeError(self.message)


class BlockingAdapter:
    """Hold one source fetch open so API run/source locks can be observed."""

    def __init__(
        self,
        started: threading.Event,
        release: threading.Event,
        calls: list[str],
    ):
        self.started = started
        self.release = release
        self.calls = calls

    def scan(self, source):
        self.calls.append(source["id"])
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("BlockingAdapter was not released by the test")
        return AdapterResult(
            content_hash=f"blocking-{source['id']}",
            snapshot_complete=False,
        )


@pytest.fixture
def radar_service(tmp_path, monkeypatch):
    db_path = tmp_path / "future-radar.db"
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_path=db_path))
    database.init_db()
    service = FutureRadarService(
        connect=database.connect,
        openai_api_key="test-key",
        ai_model="test-radar-model",
        web_search_enabled=False,
        close_confirmations=2,
        max_workers=4,
    )
    service.seed_registry()
    return service


def create_source(
    service: FutureRadarService,
    source_id: str,
    *,
    trust_level: str = "verification",
    source_type: str = "manual",
    adapter_config: dict | None = None,
):
    return service.repository.create_source({
        "id": source_id,
        "name": source_id,
        "platform": "test",
        "source_type": source_type,
        "enabled": True,
        "priority": 100,
        "trust_level": trust_level,
        "interval_minutes": 60,
        "adapter_config": adapter_config or {"adapter": "manual"},
        "query_config": {},
        "region_config": {},
        "status": "pending",
        "verification_status": "verified" if trust_level == "verification" else "unverified",
    })


def sample_job(
    external_id: str = "job-shared-001",
    *,
    title: str = "2027 校园招聘数据分析岗",
    closing_date: str = "2027-09-15",
    status: str = "open",
    verification_status: str = "pending",
):
    return {
        "external_id": external_id,
        "company": "北辰银行",
        "title": title,
        "city": "上海",
        "region": "中国大陆",
        "employer_type": "银行/金融",
        "industry": "金融",
        "official_url": f"https://example.com/campus/jobs/{external_id}",
        "application_url": f"https://example.com/campus/jobs/{external_id}/apply",
        "opening_date": "2027-07-01",
        "closing_date": closing_date,
        "status": status,
        "verification_status": verification_status,
        "confidence_score": 0.7,
        "requirements": "面向 2027 届毕业生。",
        "tags": ["校园招聘", "2027届"],
        "evidence": ["公开页面明确列出该岗位。"],
    }


def sample_program(
    *,
    verification_status: str = "pending",
    official_url: str = "https://example.com/campus/2027",
):
    return {
        "external_id": "program-shared-2027",
        "company": "北辰银行",
        "program_name": "2027 全球校园招聘",
        "recruitment_year": 2027,
        "recruitment_type": "autumn",
        "region": "中国大陆及香港",
        "status": "open",
        "verification_status": verification_status,
        "confidence_score": 0.65,
        "official_url": official_url,
        "evidence": ["公开来源宣布 2027 校园招聘启动。"],
    }


def table_count(service: FutureRadarService, table: str) -> int:
    with service.repository._connect() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def event_types(service: FutureRadarService) -> list[str]:
    with service.repository._connect() as connection:
        return [
            str(row[0])
            for row in connection.execute("SELECT event_type FROM radar_events ORDER BY id")
        ]


def test_structured_job_taxonomy_schema_accepts_normalizes_and_rejects():
    payload = {
        **sample_job("structured-schema-job"),
        "primary_category": "big-four-professional-services",
        "organization_category": "Professional Services",
        "industry_tags": ["Asset Management", "asset-management", "Advisory"],
        "role_tags": ["AI", "ai", "Data Science"],
        "description": "AI and data advisory graduate role.",
        "responsibilities": "Build data products and advise clients.",
    }
    parsed = RadarJobInput.model_validate(payload)
    assert parsed.primary_category == "big_four_professional_services"
    assert parsed.organization_category == "professional_services"
    assert parsed.industry_tags == ["advisory", "asset_management"]
    assert parsed.role_tags == ["ai", "data_science"]

    assert len(PRIMARY_CATEGORY_CODES) == 10
    with pytest.raises(ValidationError, match="primary_category"):
        RadarJobInput.model_validate({**payload, "primary_category": "unknown_sector"})
    with pytest.raises(ValidationError, match="industry_tags"):
        RadarJobInput.model_validate({**payload, "industry_tags": "asset_management"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RadarJobInput.model_validate({**payload, "company_tier": "T0"})


def test_structured_job_taxonomy_normalization_and_hash_are_stable():
    first = normalize_job({
        **sample_job("structured-hash-job"),
        "primary_category": "quant_private_hedge",
        "organization_category": "Private Fund",
        "industry_tags": ["Quant", "private fund", "QUANT"],
        "role_tags": ["Data Science", "AI", "data-science"],
        "description": "  Systematic   investment research. ",
        "responsibilities": "Research signals.\nBuild models.",
    })
    second = normalize_job({
        **sample_job("structured-hash-job"),
        "primary_category": "quant_private_hedge",
        "organization_category": "private_fund",
        "industry_tags": ["PRIVATE-FUND", "quant"],
        "role_tags": ["ai", "data science"],
        "description": "Systematic investment research.",
        "responsibilities": "Research signals. Build models.",
    })
    assert first["organization_category"] == "private_fund"
    assert first["industry_tags"] == ["private_fund", "quant"]
    assert first["role_tags"] == ["ai", "data_science"]
    assert first["industry_tags"] == second["industry_tags"]
    assert first["role_tags"] == second["role_tags"]
    assert first["content_hash"] == second["content_hash"]

    changed = normalize_job({**second, "description": "Different verified duties."})
    assert changed["content_hash"] != first["content_hash"]


def test_structured_job_taxonomy_repository_roundtrip_and_idempotent_migration(
    radar_service,
):
    source = create_source(radar_service, "structured-taxonomy-source")
    initial = {
        **sample_job("structured-roundtrip-job"),
        "primary_category": "securities_public_funds_asset_management",
        "organization_category": "public_fund",
        "industry_tags": ["asset_management", "Public Fund", "asset_management"],
        "role_tags": ["investment research", "AI", "ai"],
        "description": "Investment research role with an AI focus.",
        "responsibilities": "Research funds and build analytical tools.",
    }
    updated = {
        **initial,
        "role_tags": ["investment_research", "ai", "risk"],
        "responsibilities": "Research funds, build tools, and model risk.",
    }
    adapter = SequenceAdapter([
        AdapterResult(jobs=[initial], content_hash="structured-roundtrip-v1"),
        AdapterResult(jobs=[updated], content_hash="structured-roundtrip-v2"),
    ])
    radar_service.adapter_factory = lambda _source: adapter

    assert radar_service.run(source_ids=[source["id"]], force=True)["new_jobs"] == 1
    first = radar_service.repository.get_job("structured-roundtrip-job")
    assert first["primary_category"] == "securities_public_funds_asset_management"
    assert first["organization_category"] == "public_fund"
    assert first["industry_tags"] == ["asset_management", "public_fund"]
    assert first["role_tags"] == ["ai", "investment_research"]
    assert first["description"] == "Investment research role with an AI focus."
    assert first["responsibilities"] == "Research funds and build analytical tools."

    assert radar_service.run(source_ids=[source["id"]], force=True)["updated_jobs"] == 1
    listed = radar_service.repository.list_jobs(
        page=1, page_size=10, filters={"status": "all", "active_only": False}
    )["items"]
    stored = next(item for item in listed if item["external_id"] == "structured-roundtrip-job")
    assert stored["role_tags"] == ["ai", "investment_research", "risk"]
    assert stored["responsibilities"] == "Research funds, build tools, and model risk."

    with radar_service.repository._connect() as connection:
        migrate(connection)
        migrate(connection)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(radar_jobs)")
        }
        assert {
            "primary_category", "organization_category", "industry_tags", "role_tags",
            "description", "responsibilities",
        }.issubset(columns)
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE version='future_radar_v2_job_taxonomy'"
        ).fetchone()[0] == 1


def test_structured_job_taxonomy_migration_upgrades_legacy_job_table():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE radar_jobs (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL UNIQUE,
            program_id TEXT,
            company_id TEXT,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            city TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            employer_type TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            official_url TEXT,
            application_url TEXT,
            opening_date TEXT,
            closing_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            verification_status TEXT NOT NULL DEFAULT 'pending',
            confidence_score REAL NOT NULL DEFAULT 0,
            requirements TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            content_hash TEXT NOT NULL,
            source_id TEXT,
            missing_successes INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    timestamps = ("2026-08-28T00:00:00+00:00",) * 5
    connection.execute(
        """
        INSERT INTO radar_jobs
            (id, external_id, company, title, tags, content_hash,
             first_seen_at, last_seen_at, last_changed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-tag-row", "legacy-tag-row", "Legacy Co", "Graduate Role",
            '["legacy-compatible","quant_private_hedge"]', "legacy-hash",
            *timestamps,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_jobs
            (id, external_id, company, title, employer_type, industry, tags,
             content_hash, first_seen_at, last_seen_at, last_changed_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-alias-row", "legacy-alias-row", "Legacy Tech", "Graduate Role",
            "互联网企业", "人工智能", '["校园招聘"]', "legacy-alias-hash",
            *timestamps,
        ),
    )
    try:
        migrate(connection)
        assert connection.execute(
            "SELECT primary_category FROM radar_jobs WHERE id='legacy-tag-row'"
        ).fetchone()[0] == "quant_private_hedge"
        assert connection.execute(
            "SELECT primary_category FROM radar_jobs WHERE id='legacy-alias-row'"
        ).fetchone()[0] == "internet_tech"
        connection.execute(
            """
            INSERT INTO radar_jobs
                (id, external_id, company, title, organization_category, tags,
                 content_hash, first_seen_at, last_seen_at, last_changed_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-org-row", "legacy-org-row", "Legacy Org", "Graduate Role",
                "big_four_professional_services", "legacy-org-hash", *timestamps,
            ),
        )
        migrate(connection)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(radar_jobs)")
        }
        assert {
            "primary_category", "organization_category", "industry_tags", "role_tags",
            "description", "responsibilities",
        }.issubset(columns)
        assert connection.execute(
            "SELECT primary_category FROM radar_jobs WHERE id='legacy-org-row'"
        ).fetchone()[0] == "big_four_professional_services"
    finally:
        connection.close()


def test_legacy_adapter_persists_metadata_categories_and_repository_pages_them(
    radar_service, monkeypatch
):
    rows = [
        {
            "id": "legacy-public-fund", "company": "示例机构甲", "title": "投研岗",
            "employer_type": "券商/公募/资管", "industry": "资产管理",
            "url": "https://example.com/legacy/public", "tags": ["校园招聘"],
            "requirements": "面向应届生", "status": "open",
        },
        {
            "id": "legacy-explicit", "company": "示例机构乙", "title": "量化岗",
            "employer_type": "券商/公募/资管", "industry": "资产管理",
            "primary_category": "quant_private_hedge",
            "url": "https://example.com/legacy/quant", "tags": ["校园招聘"],
            "requirements": "面向应届生", "status": "open",
        },
        {
            "id": "legacy-tag-code", "company": "示例机构丙", "title": "咨询岗",
            "employer_type": "其他", "industry": "其他",
            "url": "https://example.com/legacy/advisory",
            "tags": ["校园招聘", "big_four_professional_services"],
            "requirements": "面向应届生", "status": "open",
        },
        {
            "id": "legacy-private-fund", "company": "示例机构丁", "title": "量化研究岗",
            "employer_type": "私募证券", "industry": "资产管理",
            "industry_tags": ["asset_management", "quant", "private_fund"],
            "url": "https://example.com/legacy/private", "tags": ["校园招聘"],
            "requirements": "面向应届生", "status": "open",
        },
        {
            "id": "legacy-no-name-inference", "company": "Deloitte", "title": "咨询岗",
            "employer_type": "其他", "industry": "其他",
            "url": "https://example.com/legacy/unclassified", "tags": ["校园招聘"],
            "requirements": "面向应届生", "status": "open",
        },
    ]
    monkeypatch.setattr(database, "list_recruitment_jobs", lambda: deepcopy(rows))
    source = create_source(radar_service, "legacy-category-regression")
    radar_service.adapter_factory = lambda _source: LegacyDatabaseAdapter()
    assert radar_service.run(source_ids=[source["id"]], force=True)["new_jobs"] == 5

    assert radar_service.repository.get_job("legacy-public-fund")["primary_category"] == (
        "securities_public_funds_asset_management"
    )
    assert radar_service.repository.get_job("legacy-explicit")["primary_category"] == (
        "quant_private_hedge"
    )
    assert radar_service.repository.get_job("legacy-private-fund")["primary_category"] == (
        "quant_private_hedge"
    )
    assert radar_service.repository.get_job("legacy-no-name-inference")["primary_category"] == ""
    filters = {
        "status": "all", "active_only": False,
        "primary_categories": ["quant_private_hedge", "big_four_professional_services"],
    }
    first = radar_service.repository.list_jobs(page=1, page_size=1, filters=filters)
    second = radar_service.repository.list_jobs(page=2, page_size=1, filters=filters)
    assert first["total"] == second["total"] == 3
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_adapter_job_without_explicit_primary_is_persisted_and_queryable(
    radar_service,
):
    source = create_source(radar_service, "implicit-primary-category")
    job = {
        **sample_job("implicit-internet-job"),
        "employer_type": "互联网企业",
        "industry": "人工智能/云计算",
        "tags": ["校园招聘"],
    }
    radar_service.adapter_factory = lambda _source: StaticAdapter(
        AdapterResult(jobs=[job], content_hash="implicit-category-v1")
    )
    assert radar_service.run(source_ids=[source["id"]], force=True)["new_jobs"] == 1
    stored = radar_service.repository.get_job("implicit-internet-job")
    assert stored["primary_category"] == "internet_tech"
    result = radar_service.repository.list_jobs(
        page=1,
        page_size=10,
        filters={
            "status": "all",
            "active_only": False,
            "primary_categories": ["internet_tech"],
        },
    )
    assert result["total"] == 1
    assert result["items"][0]["external_id"] == "implicit-internet-job"


def test_verified_structured_job_fields_survive_incomplete_discovery_merge():
    existing = normalize_job({
        **sample_job("merge-protected"),
        "verification_status": "verified",
        "primary_category": "big_four_professional_services",
        "organization_category": "professional_services",
        "industry_tags": ["professional_services"],
        "role_tags": ["ai", "technology_consulting"],
        "description": "Verified description",
        "responsibilities": "Verified responsibilities",
        "requirements": "Verified requirements",
        "tags": ["official"],
    })
    incoming = normalize_job({
        **sample_job("merge-protected"),
        "primary_category": "quant_private_hedge",
        "organization_category": "private_fund",
        "industry_tags": ["private_fund"],
        "role_tags": ["sales"],
        "description": "",
        "responsibilities": "",
        "requirements": "",
        "tags": ["mirror"],
    })
    merged = FutureRadarService._merge_verified(
        existing, incoming, incoming_role="discovery"
    )
    for field in (
        "primary_category", "organization_category", "industry_tags", "role_tags",
        "description", "responsibilities", "requirements",
    ):
        assert merged[field] == existing[field]
    assert merged["verification_status"] == "verified"


def test_mock_five_round_lifecycle_and_repeated_round_is_idempotent(radar_service):
    source_id = "mock-future-radar"
    radar_service.repository.patch_source(
        source_id,
        {"enabled": True, "adapter_config": {"adapter": "mock", "round": 1}},
    )

    round1 = radar_service.run(source_ids=[source_id], force=True)
    assert round1["status"] == "success"
    assert round1["new_jobs"] == 10
    assert table_count(radar_service, "radar_jobs") == 10
    assert event_types(radar_service).count("NEW") == 10

    repeated = radar_service.run(source_ids=[source_id], force=True)
    assert repeated["new_jobs"] == 0
    assert repeated["updated_jobs"] == 0
    assert repeated["closed_jobs"] == 0
    assert repeated["unchanged_jobs"] == 10
    assert table_count(radar_service, "radar_jobs") == 10
    assert event_types(radar_service).count("NEW") == 10

    radar_service.repository.patch_source(
        source_id, {"adapter_config": {"adapter": "mock", "round": 2}}
    )
    round2 = radar_service.run(source_ids=[source_id], force=True)
    assert round2["new_jobs"] == 2
    assert table_count(radar_service, "radar_jobs") == 12

    radar_service.repository.patch_source(
        source_id, {"adapter_config": {"adapter": "mock", "round": 3}}
    )
    round3 = radar_service.run(source_ids=[source_id], force=True)
    assert round3["updated_jobs"] == 1
    job1 = radar_service.repository.get_job("mock-2027-job-01")
    assert job1 is not None
    assert any(
        event["event_type"] == "UPDATED" and "closing_date" in event["changed_fields"]
        for event in job1["events"]
    )

    radar_service.repository.patch_source(
        source_id, {"adapter_config": {"adapter": "mock", "round": 4}}
    )
    round4 = radar_service.run(source_ids=[source_id], force=True)
    assert round4["closed_jobs"] == 1
    assert radar_service.repository.get_job("mock-2027-job-02")["status"] == "closed"

    radar_service.repository.patch_source(
        source_id, {"adapter_config": {"adapter": "mock", "round": 5}}
    )
    round5 = radar_service.run(source_ids=[source_id], force=True)
    assert round5["reopened_jobs"] == 1
    assert radar_service.repository.get_job("mock-2027-job-02")["status"] == "open"
    assert "REOPENED" in event_types(radar_service)


def test_two_successful_missing_snapshots_are_required_before_close(radar_service):
    source = create_source(
        radar_service,
        "missing-confirmation-source",
        adapter_config={"adapter": "test", "close_confirmations": 2},
    )
    adapter = SequenceAdapter([
        AdapterResult(jobs=[sample_job("missing-job")], content_hash="snapshot-1"),
        AdapterResult(jobs=[], content_hash="snapshot-2", snapshot_complete=True),
        AdapterResult(jobs=[], content_hash="snapshot-3", snapshot_complete=True),
    ])
    radar_service.adapter_factory = lambda _source: adapter

    first = radar_service.run(source_ids=[source["id"]], force=True)
    assert first["new_jobs"] == 1
    second = radar_service.run(source_ids=[source["id"]], force=True)
    assert second["closed_jobs"] == 0
    assert radar_service.repository.get_job("missing-job")["status"] == "open"
    third = radar_service.run(source_ids=[source["id"]], force=True)
    assert third["closed_jobs"] == 1
    assert radar_service.repository.get_job("missing-job")["status"] == "closed"


def test_one_adapter_failure_does_not_close_existing_job_or_stop_other_source(radar_service):
    failing_source = create_source(radar_service, "temporarily-failing-source")
    good_source = create_source(radar_service, "healthy-source")
    adapters = {
        failing_source["id"]: StaticAdapter(AdapterResult(
            jobs=[sample_job("preserved-job")], content_hash="failure-source-v1"
        )),
        good_source["id"]: StaticAdapter(AdapterResult(
            jobs=[sample_job("healthy-new-job")], content_hash="healthy-source-v1"
        )),
    }
    radar_service.adapter_factory = lambda source: adapters[source["id"]]
    assert radar_service.run(source_ids=[failing_source["id"]], force=True)["new_jobs"] == 1

    adapters[failing_source["id"]] = FailingAdapter("temporary HTTP 500")
    run = radar_service.run(
        source_ids=[failing_source["id"], good_source["id"]], force=True
    )
    assert run["status"] == "partial_success"
    assert run["sources_failed"] == 1
    assert run["sources_succeeded"] == 1
    assert run["new_jobs"] == 1
    assert radar_service.repository.get_job("preserved-job")["status"] == "open"
    assert radar_service.repository.get_job("healthy-new-job") is not None


def test_same_job_from_two_sources_merges_provenance_and_verification(radar_service):
    discovery = create_source(
        radar_service, "job-discovery-source", trust_level="discovery"
    )
    verifier = create_source(
        radar_service, "job-verification-source", trust_level="verification"
    )
    adapters = {
        discovery["id"]: StaticAdapter(AdapterResult(
            jobs=[sample_job(verification_status="pending")], content_hash="discovery-v1"
        )),
        verifier["id"]: StaticAdapter(AdapterResult(
            jobs=[sample_job(verification_status="pending")], content_hash="verification-v1"
        )),
    }
    radar_service.adapter_factory = lambda source: adapters[source["id"]]

    radar_service.run(source_ids=[discovery["id"]], force=True)
    radar_service.run(source_ids=[verifier["id"]], force=True)

    assert table_count(radar_service, "radar_jobs") == 1
    job = radar_service.repository.get_job("job-shared-001")
    assert job["verification_status"] == "verified"
    assert {source["source_id"] for source in job["sources"]} == {
        discovery["id"], verifier["id"]
    }
    assert {source["verification_role"] for source in job["sources"]} == {
        "discovery", "verification"
    }
    assert "VERIFIED" in event_types(radar_service)


def test_discovery_adapter_promotes_only_deterministically_attested_job(radar_service):
    source = create_source(
        radar_service,
        "web-search-with-official-attestation",
        trust_level="discovery",
        source_type="openai_web_search",
        adapter_config={"adapter": "openai_web_search"},
    )
    attested = sample_job("official-title-match", verification_status="verified")
    pending = sample_job("unconfirmed-search-row", verification_status="verified")
    radar_service.adapter_factory = lambda _source: StaticAdapter(AdapterResult(
        jobs=[attested, pending],
        content_hash="attested-per-item-v1",
        verified_job_external_ids={"official-title-match"},
    ))

    result = radar_service.run(source_ids=[source["id"]], force=True)

    assert result["new_jobs"] == 2
    verified = radar_service.repository.get_job("official-title-match")
    unconfirmed = radar_service.repository.get_job("unconfirmed-search-row")
    assert verified["verification_status"] == "verified"
    assert verified["sources"][0]["verification_role"] == "verification"
    assert unconfirmed["verification_status"] == "pending"
    assert unconfirmed["sources"][0]["verification_role"] == "discovery"


@pytest.mark.parametrize("discover_links", [True, False])
def test_official_link_discovery_respects_item_attestation_and_preserves_old_jobs(radar_service, discover_links):
    source = create_source(
        radar_service, "official-linked-list-test", source_type="official_html",
        adapter_config={"adapter": "official_html", "discover_job_links": discover_links},
    )
    sequence = SequenceAdapter([
        AdapterResult(jobs=[
            sample_job("linked-verified", verification_status="verified"),
            sample_job("linked-pending", verification_status="pending"),
        ], snapshot_complete=False),
        AdapterResult(snapshot_complete=False, status="partial"),
        AdapterResult(snapshot_complete=False, status="partial"),
        AdapterResult(snapshot_complete=False, status="partial"),
    ])
    seen_cancellation_checks = []

    def factory(scan_source):
        check = scan_source["adapter_config"]["_cancellation_check"]
        assert callable(check)
        check()
        seen_cancellation_checks.append(check)
        return sequence

    radar_service.adapter_factory = factory
    for _ in range(4):
        result = radar_service.run(source_ids=[source["id"]], force=True)
        assert result["closed_jobs"] == 0
    verified = radar_service.repository.get_job("linked-verified")
    pending = radar_service.repository.get_job("linked-pending")
    assert verified["verification_status"] == "verified" and verified["status"] == "open"
    assert pending["verification_status"] == ("pending" if discover_links else "verified")
    assert pending["sources"][0]["verification_role"] == ("discovery" if discover_links else "verification")
    assert pending["status"] == "open"
    assert len(seen_cancellation_checks) == 4
    assert "_cancellation_check" not in radar_service.repository.get_source(source["id"])["adapter_config"]


def test_same_program_merges_and_official_source_upgrades_verified(radar_service):
    discovery = create_source(
        radar_service, "program-discovery-source", trust_level="discovery"
    )
    verifier = create_source(
        radar_service,
        "program-official-source",
        trust_level="verification",
        source_type="official_html",
    )
    adapters = {
        discovery["id"]: StaticAdapter(AdapterResult(
            programs=[sample_program(verification_status="pending")],
            content_hash="program-discovery-v1",
        )),
        verifier["id"]: StaticAdapter(AdapterResult(
            programs=[sample_program(verification_status="pending")],
            content_hash="program-official-v1",
        )),
    }
    radar_service.adapter_factory = lambda source: adapters[source["id"]]

    radar_service.run(source_ids=[discovery["id"]], force=True)
    radar_service.run(source_ids=[verifier["id"]], force=True)

    assert table_count(radar_service, "recruitment_programs") == 1
    program = radar_service.repository.get_program("program-shared-2027")
    assert program["verification_status"] == "verified"
    assert {source["source_id"] for source in program["sources"]} == {
        discovery["id"], verifier["id"]
    }
    assert "PROGRAM_VERIFIED" in event_types(radar_service)


def test_openai_adapter_failure_degrades_and_never_persists_provider_detail(
    radar_service,
):
    ai_source = create_source(
        radar_service,
        "openai-test-source",
        trust_level="discovery",
        source_type="openai_web_search",
    )
    deterministic = create_source(radar_service, "deterministic-test-source")
    adapters = {
        ai_source["id"]: FailingAdapter(
            "credit_balance_exhausted request_id=req-private sk-private-secret"
        ),
        deterministic["id"]: StaticAdapter(AdapterResult(
            jobs=[sample_job("deterministic-job")], content_hash="deterministic-v1"
        )),
    }
    radar_service.adapter_factory = lambda source: adapters[source["id"]]

    run = radar_service.run(
        source_ids=[ai_source["id"], deterministic["id"]], force=True
    )
    assert run["status"] == "partial_success"
    assert run["sources_failed"] == 1
    assert run["sources_succeeded"] == 1
    assert radar_service.repository.get_job("deterministic-job") is not None
    assert run["errors"] == [{
        "source_id": ai_source["id"],
        "code": "AI_CREDITS_EXHAUSTED",
        "message": "AI 补漏额度暂不可用；确定性官网信源仍会继续扫描。",
    }]
    stored = radar_service.repository.get_source(ai_source["id"])
    assert stored["last_error"] == run["errors"][0]["message"]
    assert "req-private" not in stored["last_error"]
    assert "sk-private" not in stored["last_error"]


def test_frostfire_sync_v1_is_idempotent_and_rejects_key_reuse(radar_service):
    payload = FrostFireSyncV1.model_validate({
        "version": "FROSTFIRE_SYNC_V1",
        "batch_id": "batch-one",
        "source_id": "external-sync-source",
        "source_name": "External Test Source",
        "snapshot_complete": False,
        "jobs": [sample_job("sync-job")],
    }).model_dump(mode="json")

    first = radar_service.sync(payload, idempotency_key="sync-key-one")
    assert first["idempotent_replay"] is False
    assert first["counts"]["new_jobs"] == 1
    repeated = radar_service.sync(payload, idempotency_key="sync-key-one")
    assert repeated["idempotent_replay"] is True
    assert table_count(radar_service, "radar_jobs") == 1
    assert event_types(radar_service).count("NEW") == 1

    changed = deepcopy(payload)
    changed["jobs"][0]["title"] = "2027 校园招聘风险数据岗"
    with pytest.raises(SyncConflict, match="different payload"):
        radar_service.sync(changed, idempotency_key="sync-key-one")

    with pytest.raises(ValidationError):
        FrostFireSyncV1.model_validate({**payload, "cookie": "must-not-be-accepted"})


def test_semantically_irrelevant_html_and_whitespace_changes_do_not_emit_updated(radar_service):
    source = create_source(radar_service, "semantic-html-source")
    base = sample_job("semantic-job")
    changed_markup = deepcopy(base)
    changed_markup["title"] = "  2027   校园招聘数据分析岗  "
    changed_markup["requirements"] = "面向 2027 届毕业生。\n"
    assert normalize_html_text("<main>岗位 A</main>") == normalize_html_text(
        "<main>  岗位   A </main><!-- decorative change -->"
    )
    assert normalize_job(base)["content_hash"] == normalize_job(changed_markup)["content_hash"]

    adapter = SequenceAdapter([
        AdapterResult(jobs=[base], content_hash="raw-html-hash-1"),
        AdapterResult(jobs=[changed_markup], content_hash="raw-html-hash-2"),
    ])
    radar_service.adapter_factory = lambda _source: adapter
    radar_service.run(source_ids=[source["id"]], force=True)
    second = radar_service.run(source_ids=[source["id"]], force=True)
    assert second["updated_jobs"] == 0
    assert second["unchanged_jobs"] == 1
    assert event_types(radar_service) == ["NEW"]


def test_wechat_discovery_limited_is_reported_without_fabricated_success(radar_service):
    source_id = "wechat-guoyang-campus"
    run = radar_service.run(source_ids=[source_id], force=True)
    assert run["status"] == "failed"
    assert run["sources_succeeded"] == 0
    assert run["sources_failed"] == 1
    assert run["errors"][0]["code"] == "DISCOVERY_LIMITED"
    source = radar_service.repository.get_source(source_id)
    assert source["status"] == "discovery_limited"
    assert source["last_success_at"] is None
    assert table_count(radar_service, "radar_jobs") == 0
    assert table_count(radar_service, "source_articles") == 0
    assert table_count(radar_service, "radar_events") == 0


def test_official_registry_uses_exact_public_markers_and_never_generic_jobs(
    radar_service,
):
    source_ids = {
        source["id"] for source in radar_service.repository.list_sources(enabled=True)
    }
    assert {
        "official-dji-digital-2027",
        "official-pdd-campus-2027",
        "official-honor-campus-2027",
        "official-china-telecom-campus-2027",
        "official-haier-campus-2027",
        "official-xiaomi-campus-2027",
        "official-xiaomi-top-talent-2027",
    }.issubset(source_ids)
    for source_id in source_ids:
        if not source_id.startswith("official-"):
            continue
        source = radar_service.repository.get_source(source_id)
        assert source["trust_level"] == "verification"
        config = source["adapter_config"]
        if config["adapter"] == "official_api":
            assert {
                "official-china-unicom-campus-2027": "china_unicom_campus",
                "official-china-telecom-campus-jobs-2027": "china_telecom_campus",
                "official-china-mobile-campus-notices": "china_mobile_notices",
            }.get(source_id) == config.get("provider")
            assert source["url"].startswith("https://")
            assert config["ai_extract"] is False
        else:
            assert config["adapter"] == "official_html"
            assert config["required_markers"]

    # Program overview pages without an exact position do not manufacture a
    # generic "2027 campus recruitment" job.
    for source_id in (
        "official-pdd-campus-2027",
        "official-china-telecom-campus-2027",
        "official-haier-campus-2027",
        "official-xiaomi-campus-2027",
    ):
        assert "job_title" not in radar_service.repository.get_source(source_id)[
            "adapter_config"
        ]


def test_seed_registry_upgrades_controlled_config_on_existing_database(
    radar_service,
):
    source_id = "official-dji-digital-2027"
    with radar_service.repository.transaction() as connection:
        connection.execute(
            """
            UPDATE monitor_sources SET
                url='https://example.com/obsolete', domain='example.com',
                enabled=0, priority=1, trust_level='discovery',
                interval_minutes=999, adapter_config='{"adapter":"official_html"}',
                query_config='{}', region_config='{}',
                verification_status='unverified', status='error',
                last_error='safe prior operational error'
            WHERE id=?
            """,
            (source_id,),
        )

    radar_service.seed_registry()
    upgraded = radar_service.repository.get_source(source_id)
    assert upgraded["url"] == "https://careers.dji.com/zh-CN/campus/digital-recruitment"
    assert upgraded["domain"] == "careers.dji.com"
    assert upgraded["enabled"] is True
    assert upgraded["priority"] == 95
    assert upgraded["trust_level"] == "verification"
    assert upgraded["interval_minutes"] == 60
    assert upgraded["adapter_config"]["required_markers"] == [
        "2027", "数字管理构建者计划"
    ]
    assert upgraded["query_config"] == {
        "recruitment_year": 2027, "scope": "campus"
    }
    assert upgraded["region_config"]["timezone"] == "Asia/Shanghai"
    assert upgraded["verification_status"] == "verified"
    # Runtime health belongs to the scanner, not seed configuration.
    assert upgraded["status"] == "error"
    assert upgraded["last_error"] == "safe prior operational error"


def test_official_html_emits_only_positions_present_on_verified_overview(
    monkeypatch, radar_service
):
    source = radar_service.repository.get_source("official-honor-campus-2027")
    page = SimpleNamespace(
        final_url="https://www.honor.com/cn/career/?volatile=ignored",
        fingerprint="honor-public-page-v1",
        keyword_hits=["校园招聘"],
        text=(
            "荣耀 荣耀2027届校园招聘全球启动 "
            "机器人感知算法工程师 大模型算法工程师 "
            "AIGC 图像视频生成算法工程师"
        ),
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters.fetch_watch_page",
        lambda *_args, **_kwargs: page,
    )
    result = OfficialHtmlAdapter(
        repository=radar_service.repository,
        api_key="test-key",
        ai_model="test-model",
    ).scan(source)
    assert len(result.programs) == 1
    assert {job["title"] for job in result.jobs} == {
        "机器人感知算法工程师",
        "大模型算法工程师",
        "AIGC 图像视频生成算法工程师",
    }
    assert all(job["verification_status"] == "verified" for job in result.jobs)
    assert all(job["primary_category"] == "internet_tech" for job in result.jobs)
    assert all(job["official_url"] == source["url"].rstrip("/") for job in result.jobs)
    # The direct ATS pages are JavaScript shells whose job text cannot be
    # verified by our deterministic fetcher.  Cards therefore open the
    # verified official overview instead of advertising an unverified link.
    assert all(job["application_url"] == source["url"].rstrip("/") for job in result.jobs)
    assert not any(job["title"] == "2027 届校园招聘" for job in result.jobs)

    # A generic campus shell without the exact campaign markers is a complete
    # empty observation, not a fabricated vacancy.
    page.text = "荣耀 校园招聘"
    missing = OfficialHtmlAdapter(
        repository=radar_service.repository,
        api_key="test-key",
        ai_model="test-model",
    ).scan(source)
    assert missing.programs == []
    assert missing.jobs == []


def test_xiaomi_top_talent_emits_only_the_named_official_project(
    monkeypatch, radar_service
):
    source = radar_service.repository.get_source("official-xiaomi-top-talent-2027")
    page = SimpleNamespace(
        final_url=source["url"],
        fingerprint="xiaomi-top-talent-v1",
        keyword_hits=["顶尖应届生项目"],
        text=(
            "小米 全球 顶尖人才 顶尖应届生项目 2024年-2027年 "
            "面向全球顶尖高校应届生"
        ),
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters.fetch_watch_page",
        lambda *_args, **_kwargs: page,
    )
    result = OfficialHtmlAdapter(
        repository=radar_service.repository,
        api_key="test-key",
        ai_model="test-model",
    ).scan(source)
    assert len(result.programs) == 1
    assert [job["title"] for job in result.jobs] == ["顶尖应届生项目"]
    assert result.jobs[0]["application_url"] == source["url"]
    assert result.jobs[0]["verification_status"] == "verified"


def test_official_html_marks_configured_expired_position_closed(
    monkeypatch, radar_service
):
    source = deepcopy(radar_service.repository.get_source("official-honor-campus-2027"))
    source["adapter_config"]["configured_jobs"] = [{
        **source["adapter_config"]["configured_jobs"][0],
        "closing_date": "2020-01-01",
    }]
    page = SimpleNamespace(
        final_url=source["url"],
        fingerprint="honor-expired-page",
        keyword_hits=["校园招聘"],
        text="荣耀 荣耀2027届校园招聘全球启动 机器人感知算法工程师",
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters.fetch_watch_page",
        lambda *_args, **_kwargs: page,
    )
    result = OfficialHtmlAdapter(
        repository=radar_service.repository,
        api_key="test-key",
        ai_model="test-model",
    ).scan(source)
    assert len(result.jobs) == 1
    assert result.jobs[0]["status"] == "closed"


def test_same_content_hash_still_applies_time_driven_status_transition(
    radar_service,
):
    source = create_source(radar_service, "same-page-date-transition")
    adapter = SequenceAdapter([
        AdapterResult(
            jobs=[sample_job(
                "same-page-date-job", closing_date="2099-12-31", status="open"
            )],
            content_hash="unchanged-official-page",
        ),
        AdapterResult(
            jobs=[sample_job(
                "same-page-date-job", closing_date="2020-01-01", status="closed"
            )],
            content_hash="unchanged-official-page",
        ),
    ])
    radar_service.adapter_factory = lambda _source: adapter

    first = radar_service.run(source_ids=[source["id"]], force=True)
    assert first["new_jobs"] == 1
    assert radar_service.repository.get_job("same-page-date-job")["status"] == "open"

    second = radar_service.run(source_ids=[source["id"]], force=True)
    assert second["closed_jobs"] == 1
    transitioned = radar_service.repository.get_job("same-page-date-job")
    assert transitioned["status"] == "closed"
    assert transitioned["closing_date"] == "2020-01-01"


def test_official_html_rejects_ambiguous_marker_configuration(
    monkeypatch, radar_service
):
    source = deepcopy(radar_service.repository.get_source("official-honor-campus-2027"))
    page = SimpleNamespace(
        final_url=source["url"],
        fingerprint="honor-invalid-config",
        keyword_hits=["校园招聘"],
        text="荣耀 荣耀2027届校园招聘全球启动 机器人感知算法工程师",
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters.fetch_watch_page",
        lambda *_args, **_kwargs: page,
    )
    adapter = OfficialHtmlAdapter(
        repository=radar_service.repository,
        api_key="test-key",
        ai_model="test-model",
    )
    source["adapter_config"]["required_markers"] = "荣耀2027届校园招聘全球启动"
    with pytest.raises(ValueError, match="required_markers"):
        adapter.scan(source)


def test_wechat_public_metadata_is_preserved_when_article_body_is_unavailable(
    radar_service,
):
    source = create_source(
        radar_service,
        "wechat-public-metadata-source",
        trust_level="discovery",
        source_type="wechat_public",
        adapter_config={
            "adapter": "wechat_public",
            "article_title": "某企业 2027 校园招聘启动",
            "recruitment_year": 2027,
            "search_excerpt": "公开搜索摘要显示校园招聘已经启动。",
        },
    )
    radar_service.repository.patch_source(
        source["id"],
        {"url": "https://example.com/public-article", "domain": "example.com"},
    )
    adapter = WechatSourceAdapter(
        repository=radar_service.repository,
        api_key="test-key",
        ai_model="test-model",
    )

    def unavailable(_source):
        raise RuntimeError("body unavailable")

    adapter.html.scan = unavailable
    radar_service.adapter_factory = lambda _source: adapter

    run = radar_service.run(source_ids=[source["id"]], force=True)
    assert run["status"] == "success"
    assert run["articles_discovered"] == 1
    assert table_count(radar_service, "source_articles") == 1
    assert "ARTICLE_DISCOVERED" in event_types(radar_service)
    refreshed = radar_service.repository.get_source(source["id"])
    assert refreshed["status"] == "discovery_only"
    assert refreshed["last_success_at"] is not None


def test_wechat_web_search_discovers_public_article_and_attests_official_job(
    monkeypatch,
):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    opening_date = date.today().isoformat()
    closing_date = f"{target_year}-12-31"
    payload = {
        "articles": [{
            "title": f"拼多多 {target_year} 届校园招聘启动",
            "url": "https://mp.weixin.qq.com/s/public-article-id",
            "publish_date": date.today().isoformat(),
            "excerpt": f"面向 {target_year} 届毕业生的校园招聘。",
        }],
        "jobs": [{
            "company": "拼多多",
            "title": f"{target_year}届校园招聘产品策略岗",
            "city": "上海",
            "industry": "互联网",
            "official_url": "https://careers.pddglobalhr.com/campus/product",
            "opening_date": opening_date,
            "closing_date": closing_date,
            "requirements": f"面向{target_year}届毕业生",
            "category": "互联网企业",
        }],
    }

    class FakeResponses:
        def create(self, **kwargs):
            assert kwargs["tools"][0]["type"] == "web_search"
            assert kwargs["tool_choice"] == "required"
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[{
                        "type": "url",
                        "url": payload["articles"][0]["url"],
                    }]),
                )],
                usage=SimpleNamespace(total_tokens=321),
            )

    monkeypatch.setattr(
        "backend.future_radar.adapters.OpenAI",
        lambda **_kwargs: SimpleNamespace(responses=FakeResponses()),
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters._inspect_official_candidate_page",
        lambda job: SimpleNamespace(
            readable=True,
            closed=False,
            title_confirmed=True,
            page_text=(
                f"{job['title']}，申请开始：{opening_date}，"
                f"投递截止：{closing_date}，立即申请。"
            ),
        ),
    )
    adapter = WechatWebSearchAdapter(api_key="test-key", ai_model="test-model")
    result = adapter.scan({"name": "国央校招", "account_name": "国央校招"})

    assert len(result.articles) == 1
    assert result.articles[0]["classification"] == "recruitment_signal"
    assert len(result.jobs) == 1
    assert result.jobs[0]["verification_status"] == "verified"
    assert result.jobs[0]["opening_date"] == opening_date
    assert result.jobs[0]["closing_date"] == closing_date
    assert "链接已验证" in result.jobs[0]["tags"]
    assert result.jobs[0]["external_id"] in result.verified_job_external_ids
    assert result.ai_calls == 1
    assert result.model_tokens_used == 321


def test_wechat_web_search_preserves_unreadable_and_unverified_jobs_as_pending(
    monkeypatch,
):
    target_year = date.today().year + (1 if date.today().month >= 6 else 0)
    opening_date = date.today().isoformat()
    closing_date = f"{target_year}-12-31"
    payload = {
        "articles": [],
        "jobs": [
            {
                "company": "拼多多",
                "title": f"{target_year}届校园招聘暂不可读岗位",
                "city": "上海",
                "industry": "互联网",
                "official_url": "https://careers.pddglobalhr.com/campus/unreadable",
                "opening_date": opening_date,
                "closing_date": closing_date,
                "requirements": f"面向{target_year}届毕业生",
                "category": "互联网企业",
            },
            {
                "company": "拼多多",
                "title": f"{target_year}届校园招聘证据不足岗位",
                "city": "北京",
                "industry": "互联网",
                "official_url": "https://careers.pddglobalhr.com/campus/unverified",
                "opening_date": opening_date,
                "closing_date": closing_date,
                "requirements": f"面向{target_year}届毕业生",
                "category": "互联网企业",
            },
            {
                "company": "拼多多",
                "title": f"{target_year}届校园招聘已关闭岗位",
                "city": "深圳",
                "industry": "互联网",
                "official_url": "https://careers.pddglobalhr.com/campus/closed",
                "opening_date": opening_date,
                "closing_date": closing_date,
                "requirements": f"面向{target_year}届毕业生",
                "category": "互联网企业",
            },
        ],
    }

    class FakeResponses:
        def create(self, **_kwargs):
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="completed",
                    action=SimpleNamespace(sources=[]),
                )],
                usage=SimpleNamespace(total_tokens=432),
            )

    def inspect(job):
        if "暂不可读" in job["title"]:
            return SimpleNamespace(
                readable=False,
                closed=False,
                title_confirmed=False,
                page_text="",
            )
        if "已关闭" in job["title"]:
            return SimpleNamespace(
                readable=True,
                closed=True,
                title_confirmed=False,
                page_text="职位已关闭",
            )
        return SimpleNamespace(
            readable=True,
            closed=False,
            title_confirmed=False,
            # Both values occur, but only as publication/event dates.  They
            # must not survive as application opening/closing dates.
            page_text=(
                f"发布日期：{opening_date}；校园宣讲活动日期：{closing_date}。"
            ),
        )

    monkeypatch.setattr(
        "backend.future_radar.adapters.OpenAI",
        lambda **_kwargs: SimpleNamespace(responses=FakeResponses()),
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters._inspect_official_candidate_page", inspect,
    )

    result = WechatWebSearchAdapter(
        api_key="test-key", ai_model="test-model"
    ).scan({"name": "国央校招", "account_name": "国央校招"})

    assert len(result.jobs) == 2
    assert result.verified_job_external_ids == set()
    by_title = {job["title"]: job for job in result.jobs}
    unreadable = by_title[f"{target_year}届校园招聘暂不可读岗位"]
    unverified = by_title[f"{target_year}届校园招聘证据不足岗位"]

    assert unreadable["verification_status"] == "pending"
    assert unreadable["opening_date"] is None
    assert unreadable["closing_date"] is None
    assert "官方页暂不可读" in unreadable["tags"]
    assert "链接已验证" not in unreadable["tags"]
    assert "链接可访问" not in unreadable["tags"]

    assert unverified["verification_status"] == "pending"
    assert unverified["opening_date"] is None
    assert unverified["closing_date"] is None
    assert "链接可访问" in unverified["tags"]
    assert "待官方核验" in unverified["tags"]
    assert "链接已验证" not in unverified["tags"]
    assert all("已关闭岗位" not in title for title in by_title)


def test_wechat_web_search_rejects_json_without_completed_search_call(monkeypatch):
    payload = {
        "articles": [{
            "title": "某企业 2027 届校园招聘",
            "url": "https://public.example.com/campus/2027",
            "publish_date": None,
            "excerpt": "看似完整但没有真实工具调用的模型输出。",
        }],
        "jobs": [],
    }

    class FakeResponses:
        def create(self, **kwargs):
            assert kwargs["tool_choice"] == "required"
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[SimpleNamespace(
                    type="web_search_call",
                    status="failed",
                    action=SimpleNamespace(sources=[{
                        "url": "https://public.example.com/campus/2027",
                    }]),
                )],
                usage=SimpleNamespace(total_tokens=100),
            )

    monkeypatch.setattr(
        "backend.future_radar.adapters.OpenAI",
        lambda **_kwargs: SimpleNamespace(responses=FakeResponses()),
    )
    adapter = WechatWebSearchAdapter(api_key="test-key", ai_model="test-model")

    with pytest.raises(RuntimeError, match="did not complete"):
        adapter.scan({"name": "国央校招", "account_name": "国央校招"})


def test_wechat_web_search_articles_require_cited_url_or_same_hostname(monkeypatch):
    payload = {
        "articles": [
            {
                "title": "某企业 2027 届校园招聘启动",
                "url": "https://public.example.com/articles/campus-2027?utm_source=model",
                "publish_date": None,
                "excerpt": "面向 2027 届毕业生。",
            },
            {
                "title": "某银行 2027 届校园招聘公告",
                "url": "https://bank.example.org/campus/2027",
                "publish_date": None,
                "excerpt": "公开招聘公告。",
            },
            {
                "title": "未被搜索来源支持的 2027 届校园招聘",
                "url": "https://unsupported.example.net/campus/2027",
                "publish_date": None,
                "excerpt": "该 URL 只存在于模型 JSON。",
            },
        ],
        "jobs": [],
    }

    class FakeResponses:
        def create(self, **kwargs):
            return SimpleNamespace(
                output_text=__import__("json").dumps(payload),
                output=[{
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"sources": [
                        {
                            "type": "url",
                            "url": "https://public.example.com/articles/campus-2027",
                        },
                        SimpleNamespace(
                            type="url", url="https://bank.example.org/search/result"
                        ),
                        {"type": "url", "url": "http://127.0.0.1/private"},
                    ]},
                }],
                usage=SimpleNamespace(total_tokens=200),
            )

    monkeypatch.setattr(
        "backend.future_radar.adapters.OpenAI",
        lambda **_kwargs: SimpleNamespace(responses=FakeResponses()),
    )
    result = WechatWebSearchAdapter(
        api_key="test-key", ai_model="test-model"
    ).scan({"name": "国央校招", "account_name": "国央校招"})

    assert [article["article_url"] for article in result.articles] == [
        "https://public.example.com/articles/campus-2027",
        "https://bank.example.org/campus/2027",
    ]
    assert result.status == "healthy"
    assert result.ai_calls == 1


def test_openai_web_search_is_not_a_complete_source_snapshot(monkeypatch):
    monkeypatch.setattr(
        "backend.future_radar.adapters.search_current_recruitment_jobs",
        lambda: SimpleNamespace(jobs=[], tool_calls=1, total_tokens=12),
    )

    result = OpenAIWebSearchAdapter().scan({})

    assert result.snapshot_complete is False


def test_public_feed_adapter_parses_rss_as_discovery_articles(monkeypatch, radar_service):
    credential_marker = "sk" + "-proj-" + "NOT_A_REAL_KEY_TEST_VALUE_123456"
    uuid_marker = "-".join(("12345678", "1234", "4123", "8123", "123456789abc"))
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>公开招聘订阅</title>
      <item>
        <title>某集团 2027 届校园招聘正式启动</title>
        <link>https://careers.example.com/campus/2027?utm_source=feed</link>
        <pubDate>Thu, 27 Aug 2026 09:30:00 GMT</pubDate>
        <description><![CDATA[<p>面向应届毕业生。联系人 test@example.com，13800138000，
        api_key={credential_marker}，
        会话 {uuid_marker}。</p>]]></description>
      </item>
      <item><title>不安全链接</title><link>http://127.0.0.1/private</link></item>
      <item><title>私有会话</title><link>https://chatgpt.com/c/private-conversation-placeholder</link></item>
      <item><title>凭证查询</title><link>https://example.com/feed?token=secret-value</link></item>
    </channel></rss>"""
    page = SimpleNamespace(raw_text=rss, text="公开招聘订阅", fingerprint="feed-hash")
    monkeypatch.setattr(
        "backend.future_radar.adapters.fetch_watch_page", lambda *_args, **_kwargs: page
    )
    source = create_source(
        radar_service,
        "public-campus-rss",
        trust_level="discovery",
        source_type="public_feed",
        adapter_config={"adapter": "public_feed", "domain_delay_seconds": 0},
    )
    radar_service.repository.patch_source(
        source["id"],
        {"url": "https://feeds.example.com/campus.xml", "domain": "feeds.example.com"},
    )

    adapter = PublicFeedAdapter()
    result = adapter.scan(radar_service.repository.get_source(source["id"]))
    assert result.jobs == []
    assert result.snapshot_complete is False
    assert len(result.articles) == 1
    article = result.articles[0]
    assert article["article_url"] == "https://careers.example.com/campus/2027"
    assert article["is_recruitment"] is True
    assert article["recruitment_year"] == 2027
    assert article["publish_time"] == "2026-08-27T09:30:00+00:00"
    serialized = str(result.articles)
    assert "test@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "sk" + "-proj-" not in serialized
    assert "12345678" not in serialized

    selected = adapter_for_source(
        radar_service.repository.get_source(source["id"]),
        repository=radar_service.repository,
        openai_api_key="test-key",
        ai_model="test-model",
    )
    assert isinstance(selected, PublicFeedAdapter)

    request = SourceCreateRequest.model_validate({
        "id": "another-public-feed",
        "name": "Another public feed",
        "platform": "rss",
        "source_type": "public_feed",
        "url": "https://feeds.example.com/another.xml",
        "trust_level": "discovery",
        "adapter_config": {"adapter": "public_feed", "max_entries": 30},
    })
    assert request.source_type == "public_feed"


def test_public_reference_url_rejects_phone_numbers():
    mobile = "138" + "0013" + "8000"
    landline = "010" + "-" + "12345678"

    assert _public_reference_url(
        f"https://public.example.com/articles/{mobile}"
    ) is None
    assert _public_reference_url(
        f"https://public.example.com/articles?contact={landline}"
    ) is None
    assert _public_reference_url(
        "https://public.example.com/articles/campus-2027"
    ) == "https://public.example.com/articles/campus-2027"


@pytest.mark.parametrize("url", [
    "https://xiaoyuan.zhaopin.com/company/KA0403315311D90000008000",
    "https://barclays.wd3.myworkdayjobs.com/en-US/External_Career_Site_Barclays/job/"
    "Sales--Trading-and-Structuring-Graduate-Programme-2027-Hong-Kong_JR-0000128133",
])
def test_public_ats_identifiers_are_not_mistaken_for_phone_contacts(url):
    assert _public_reference_url(url) == url
    assert _public_reference_url(url + "?contact=13800138000") is None
    assert _public_reference_url(url + "?token=private-token") is None


@pytest.mark.parametrize("url", [
    "https://xiaoyuan.zhaopin.com.example.com/company/KA0403315311D90000008000",
    "https://barclays.wd3.myworkdayjobs.com.example.com/job/Role_JR-0000128133",
    "https://barclays.wd3.myworkdayjobs.com/contact/13800138000",
    "https://barclays.wd3.myworkdayjobs.com/job/Call-13800138000_JR-0000128133",
    "https://barclays.wd3.myworkdayjobs.com/job/Role_JR-helpdesk-13800138000",
    "https://xiaoyuan.zhaopin.com/company/KAhelpdesk13800138000",
])
def test_ats_identifier_exception_does_not_allow_other_hosts_or_contacts(url):
    assert _public_reference_url(url) is None


def test_public_feed_adapter_rejects_dtd(monkeypatch):
    page = SimpleNamespace(
        raw_text="<!DOCTYPE rss [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><rss/>",
        text="rss",
        fingerprint="unsafe-feed",
    )
    monkeypatch.setattr(
        "backend.future_radar.adapters.fetch_watch_page", lambda *_args, **_kwargs: page
    )
    with pytest.raises(DiscoveryLimitedError, match="credentials"):
        PublicFeedAdapter().scan({
            "id": "credential-feed",
            "name": "credential-feed",
            "url": "https://feeds.example.com/private.xml?token=secret-value",
            "domain": "feeds.example.com",
            "adapter_config": {"domain_delay_seconds": 0},
        })
    with pytest.raises(ValueError, match="DTD"):
        PublicFeedAdapter().scan({
            "id": "unsafe-feed",
            "name": "unsafe-feed",
            "url": "https://feeds.example.com/unsafe.xml",
            "domain": "feeds.example.com",
            "adapter_config": {"domain_delay_seconds": 0},
        })


def test_future_radar_api_paginates_allows_user_run_and_strict_sync(
    radar_service, monkeypatch
):
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
        "admin_dashboard_token": "test-admin-dashboard-token",
        "recruitment_ingest_token": "test-recruitment-ingest-token",
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)
    radar_service.repository.patch_source(
        "mock-future-radar",
        {"enabled": True, "adapter_config": {"adapter": "mock", "round": 2}},
    )
    # Seed deterministic pagination data directly. The public scan endpoint is
    # intentionally not allowed to invoke mock/manual sources.
    seeded = radar_service.run(
        trigger_type="test_seed", source_ids=["mock-future-radar"], force=True
    )
    assert seeded["new_jobs"] == 12
    radar_service.adapter_factory = lambda _source: StaticAdapter(
        AdapterResult(content_hash="user-deterministic-refresh", snapshot_complete=False)
    )

    with TestClient(main.app) as client:
        unauthorized = client.post(
            "/api/future-radar/run",
            json={"source_ids": ["mock-future-radar"], "force": True},
        )
        assert unauthorized.status_code == 401
        admin_token_only = client.post(
            "/api/future-radar/run",
            headers={"X-Admin-Token": "test-admin-dashboard-token"},
            json={"source_ids": ["mock-future-radar"]},
        )
        assert admin_token_only.status_code == 401

        stale_consent_user = database.create_user(
            "future-radar-stale-consent",
            main.hash_password("correct-horse-123"),
        )
        stale_consent = client.post(
            "/api/future-radar/run",
            headers={
                "Authorization": (
                    "Bearer "
                    + main.create_access_token(
                        stale_consent_user["id"], stale_consent_user["username"]
                    )
                )
            },
            json={"source_ids": ["mock-future-radar"]},
        )
        assert stale_consent.status_code == 428

        registered = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-api-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        )
        assert registered.status_code == 201
        bearer = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        manual = client.post(
            "/api/future-radar/run",
            headers=bearer,
        )
        assert manual.status_code == 200
        assert manual.json()["sources_checked"] > 0
        assert manual.json()["trigger_type"] == "manual_quick"
        assert manual.json()["scan_type"] == "quick"

        rerun = client.post(
            "/api/future-radar/run",
            headers=bearer,
        )
        assert rerun.status_code == 200
        assert rerun.json()["scan_type"] == "quick"

        with database.connect() as connection:
            audit = connection.execute(
                """
                SELECT user_id, status_code FROM api_usage_events
                WHERE route='/api/future-radar/run' AND user_id=?
                ORDER BY id DESC LIMIT 2
                """,
                (registered.json()["user"]["id"],),
            ).fetchall()
        assert [row["status_code"] for row in audit] == [200, 200]

        force_user = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-force-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        ).json()
        bearer_only_force = client.post(
            "/api/future-radar/run",
            headers={"Authorization": f"Bearer {force_user['access_token']}"},
            json={"force": True},
        )
        assert bearer_only_force.status_code == 401

        admin_force = client.post(
            "/api/future-radar/run",
            headers={
                "Authorization": f"Bearer {force_user['access_token']}",
                "X-Admin-Token": "test-admin-dashboard-token",
            },
            json={"force": True},
        )
        assert admin_force.status_code == 200
        assert admin_force.json()["scan_type"] == "quick"
        assert admin_force.json()["force_scan"] is True

        busy_user = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-busy-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        ).json()
        busy_headers = {"Authorization": f"Bearer {busy_user['access_token']}"}
        assert radar_service.repository.acquire_lock(
            "future-radar-run:quick", "security-test-owner", ttl_seconds=60
        )
        try:
            busy = client.post(
                "/api/future-radar/run",
                headers=busy_headers,
            )
            assert busy.status_code == 409
        finally:
            radar_service.repository.release_lock(
                "future-radar-run:quick", "security-test-owner"
            )
        retry_after_busy = client.post(
            "/api/future-radar/run",
            headers=busy_headers,
        )
        assert retry_after_busy.status_code == 200

        page1 = client.get(
            "/api/future-radar/jobs?page=1&page_size=5&status=all", headers=bearer
        )
        page2 = client.get(
            "/api/future-radar/jobs?page=2&page_size=5&status=all", headers=bearer
        )
        assert page1.status_code == page2.status_code == 200
        assert page1.json()["total"] == 12
        assert len(page1.json()["items"]) == len(page2.json()["items"]) == 5
        assert {
            item["external_id"] for item in page1.json()["items"]
        }.isdisjoint(item["external_id"] for item in page2.json()["items"])

        filtered = client.get(
            "/api/future-radar/jobs"
            "?status=all&q=%E5%8C%97%E8%BE%B0%E9%93%B6%E8%A1%8C"
            "&source_id=mock-future-radar&event_type=NEW"
            "&opening_after=2020-01-01&closing_before=2100-01-01",
            headers=bearer,
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 3
        assert all(
            item["company"] == "北辰银行" for item in filtered.json()["items"]
        )

        strict = client.post(
            "/api/future-radar/sync",
            headers={"X-Recruitment-Token": "test-recruitment-ingest-token"},
            json={
                "version": "FROSTFIRE_SYNC_V1",
                "source_id": "strict-schema-source",
                "jobs": [],
                "unexpected": "rejected",
            },
        )
        assert strict.status_code == 422

        wrong_version = client.post(
            "/api/future-radar/sync",
            headers={"X-Recruitment-Token": "test-recruitment-ingest-token"},
            json={"version": "FROSTFIRE_SYNC_V2", "source_id": "strict-schema-source"},
        )
        assert wrong_version.status_code == 422


def test_future_radar_api_quick_run_lock_survives_refresh_and_releases_immediately(
    radar_service, monkeypatch
):
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)

    source_id = "official-dji-digital-2027"
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    radar_service.adapter_factory = lambda _source: BlockingAdapter(
        started, release, calls
    )

    with TestClient(main.app) as first_browser, TestClient(main.app) as refreshed_browser:
        registered = first_browser.post(
            "/api/auth/register",
            json={
                "username": "future-radar-refresh-lock-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        )
        assert registered.status_code == 201
        headers = {
            "Authorization": f"Bearer {registered.json()['access_token']}"
        }
        request = {"scan_type": "quick", "source_ids": [source_id]}

        with ThreadPoolExecutor(max_workers=1) as pool:
            first_run = pool.submit(
                first_browser.post,
                "/api/future-radar/run",
                headers=headers,
                json=request,
            )
            assert started.wait(timeout=3), "the first quick scan never started"

            # A second browser/process-facing request still observes the
            # database-backed lock. Refreshing the page cannot bypass it.
            duplicate = refreshed_browser.post(
                "/api/future-radar/run",
                headers=headers,
                json=request,
            )
            assert duplicate.status_code == 409
            assert "quick" in duplicate.json()["detail"].casefold()
            assert calls == [source_id]

            release.set()
            completed = first_run.result(timeout=5)

        assert completed.status_code == 200
        assert completed.json()["scan_type"] == "quick"

        # There is no post-run five-minute server cooldown. Once the run lock
        # is released, the very next request is accepted.
        radar_service.adapter_factory = lambda _source: StaticAdapter(
            AdapterResult(content_hash="immediate-quick-rerun", snapshot_complete=False)
        )
        immediate = refreshed_browser.post(
            "/api/future-radar/run",
            headers=headers,
            json=request,
        )
        assert immediate.status_code == 200
        assert immediate.json()["scan_type"] == "quick"


def test_future_radar_api_deep_run_lock_prevents_duplicate_openai_call(
    radar_service, monkeypatch
):
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)

    source_id = "openai-public-web-search"
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    radar_service.adapter_factory = lambda _source: BlockingAdapter(
        started, release, calls
    )

    with TestClient(main.app) as first_browser, TestClient(main.app) as second_browser:
        # Lifespan seeding reflects the fixture's intentionally disabled OpenAI
        # configuration, so enable this controlled fake only after both app
        # instances have started.
        radar_service.repository.patch_source(source_id, {"enabled": True})
        registered = first_browser.post(
            "/api/auth/register",
            json={
                "username": "future-radar-deep-lock-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        )
        headers = {
            "Authorization": f"Bearer {registered.json()['access_token']}"
        }
        request = {"scan_type": "deep", "source_ids": [source_id]}

        with ThreadPoolExecutor(max_workers=1) as pool:
            first_run = pool.submit(
                first_browser.post,
                "/api/future-radar/run",
                headers=headers,
                json=request,
            )
            assert started.wait(timeout=3), "the first deep scan never started"
            duplicate = second_browser.post(
                "/api/future-radar/run",
                headers=headers,
                json=request,
            )
            assert duplicate.status_code == 409
            assert "deep" in duplicate.json()["detail"].casefold()
            assert calls == [source_id]
            release.set()
            completed = first_run.result(timeout=5)

        assert completed.status_code == 200
        assert completed.json()["scan_type"] == "deep"

        # Deep Scan also has no fixed post-run cooldown. The active run/source
        # lock only denies concurrent duplicate calls.
        radar_service.adapter_factory = lambda _source: StaticAdapter(
            AdapterResult(content_hash="immediate-deep-rerun", snapshot_complete=False)
        )
        immediate = second_browser.post(
            "/api/future-radar/run",
            headers=headers,
            json=request,
        )
        assert immediate.status_code == 200


def test_future_radar_api_source_lock_skips_busy_source_but_scans_free_source(
    radar_service, monkeypatch
):
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)

    busy_source = "official-dji-digital-2027"
    free_source = "official-pdd-campus-2027"
    calls: list[str] = []
    already_released = threading.Event()
    already_released.set()
    radar_service.adapter_factory = lambda _source: BlockingAdapter(
        threading.Event(), already_released, calls
    )
    owner = "api-source-lock-test-owner"
    assert radar_service.repository.acquire_lock(
        f"future-radar-source:{busy_source}", owner, ttl_seconds=60
    )

    try:
        with TestClient(main.app) as client:
            registered = client.post(
                "/api/auth/register",
                json={
                    "username": "future-radar-source-lock-user",
                    "password": "correct-horse-123",
                    "privacy_accepted": True,
                },
            )
            headers = {
                "Authorization": f"Bearer {registered.json()['access_token']}"
            }
            response = client.post(
                "/api/future-radar/run",
                headers=headers,
                json={
                    "scan_type": "quick",
                    "source_ids": [busy_source, free_source],
                },
            )
    finally:
        radar_service.repository.release_lock(
            f"future-radar-source:{busy_source}", owner
        )

    assert response.status_code == 200
    result = response.json()
    assert result["sources_skipped"] == 1
    assert any(
        error["source_id"] == busy_source and error["code"] == "SOURCE_BUSY"
        for error in result["errors"]
    )
    assert busy_source not in calls
    assert calls == [free_source]


def test_future_radar_public_api_never_exposes_unverified_legacy_candidates(
    radar_service, monkeypatch
):
    verified = {
        **sample_job("legacy-official-verified"),
        "id": "legacy-official-verified",
        "url": "https://example.com/campus/jobs/legacy-official-verified",
        "tags": ["校园招聘", "链接已验证", "标题已验证"],
    }
    pending = {
        **sample_job("legacy-discovery-pending", title="待官网确认岗位"),
        "id": "legacy-discovery-pending",
        "url": "https://example.com/campus/jobs/legacy-discovery-pending",
        "requirements": "PRIVATE-PENDING-CONTENT-MUST-NOT-BE-PUBLIC",
        "tags": ["校园招聘", "待官方核验"],
    }
    monkeypatch.setattr(
        database, "list_recruitment_jobs", lambda: deepcopy([verified, pending])
    )
    source = radar_service.repository.get_source("legacy-recruitment-pipeline")
    assert source is not None
    radar_service.adapter_factory = lambda _source: LegacyDatabaseAdapter()
    assert radar_service.run(source_ids=[source["id"]], force=True)["new_jobs"] == 2
    assert radar_service.repository.get_job("legacy-official-verified")[
        "verification_status"
    ] == "verified"
    assert radar_service.repository.get_job("legacy-discovery-pending")[
        "verification_status"
    ] == "pending"
    discovery = create_source(
        radar_service, "public-event-discovery", trust_level="discovery"
    )
    radar_service.adapter_factory = lambda _source: StaticAdapter(AdapterResult(
        programs=[{
            **sample_program(),
            "program_name": "PRIVATE-PENDING-PROGRAM-MUST-NOT-BE-PUBLIC",
        }],
    ))
    assert radar_service.run(
        source_ids=[discovery["id"]], force=True
    )["programs_discovered"] == 1

    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)

    with TestClient(main.app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-public-safety",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        ).json()
        bearer = {"Authorization": f"Bearer {registered['access_token']}"}
        jobs = client.get(
            "/api/future-radar/jobs?status=all", headers=bearer
        )
        assert jobs.status_code == 200
        assert jobs.json()["total"] == 1
        assert [item["external_id"] for item in jobs.json()["items"]] == [
            "legacy-official-verified"
        ]
        assert client.get(
            "/api/future-radar/jobs?status=all&verification_status=pending",
            headers=bearer,
        ).status_code == 422
        assert client.get(
            "/api/future-radar/jobs/legacy-discovery-pending", headers=bearer
        ).status_code == 404

        # A later official verification must not make the earlier discovery
        # snapshot public.  Only the verification event and the entity's current
        # safe fields may leave the repository.
        verifier = create_source(
            radar_service, "public-event-verifier", trust_level="verification"
        )
        radar_service.adapter_factory = lambda _source: StaticAdapter(AdapterResult(
            programs=[{
                **sample_program(verification_status="verified"),
                "program_name": "官网已核验项目",
            }],
            jobs=[{
                **sample_job(
                    "legacy-discovery-pending",
                    title="官网已核验岗位",
                ),
                "verification_status": "verified",
                "requirements": "Current verified public requirement.",
            }],
        ))
        verified_run = radar_service.run(source_ids=[verifier["id"]], force=True)
        assert verified_run["updated_jobs"] == 1

        # Article metadata remains useful internally for discovery health, but it
        # is neither a public event nor public source-registry copy.
        with radar_service.repository.transaction() as connection:
            now = "2027-07-02T00:00:00+00:00"
            article_id, _, _ = radar_service.repository.upsert_article(
                connection,
                {
                    "article_external_id": "private-article-event",
                    "publisher": "Internal discovery feed",
                    "article_title": "PRIVATE ARTICLE TITLE",
                    "article_url": "https://example.com/private-article",
                    "publish_time": now,
                    "content_hash": "private-article-hash",
                    "raw_excerpt": "PRIVATE ARTICLE EXCERPT",
                    "is_recruitment": True,
                    "recruitment_year": 2027,
                    "classification": "recruitment",
                },
                source_id=verifier["id"],
                now=now,
            )
            radar_service.repository.insert_event(
                connection,
                run_id=verified_run["id"],
                entity_type="article",
                entity_id=article_id,
                external_id="private-article-event",
                event_type="ARTICLE_DISCOVERED",
                before=None,
                after={"article_title": "PRIVATE ARTICLE TITLE"},
                fields=["article_title"],
                source_id=verifier["id"],
                now=now,
            )

        events = client.get("/api/future-radar/events", headers=bearer).json()["items"]
        upgraded_events = [
            event for event in events
            if event.get("external_id") == "legacy-discovery-pending"
        ]
        assert [event["event_type"] for event in upgraded_events] == ["VERIFIED"]
        assert all(event["entity_type"] in {"job", "program"} for event in events)
        assert all("before_data" not in event for event in events)
        assert all("after_data" not in event for event in events)
        assert "PRIVATE-PENDING-CONTENT-MUST-NOT-BE-PUBLIC" not in str(events)
        assert "PRIVATE-PENDING-PROGRAM-MUST-NOT-BE-PUBLIC" not in str(events)
        assert "PRIVATE ARTICLE" not in str(events)
        assert upgraded_events[0]["verification_status"] == "verified"
        assert upgraded_events[0]["title"] == "官网已核验岗位"
        upgraded_program_events = [
            event for event in events
            if event.get("external_id") == "program-shared-2027"
        ]
        assert [event["event_type"] for event in upgraded_program_events] == [
            "PROGRAM_VERIFIED"
        ]
        assert upgraded_program_events[0]["program_name"] == "官网已核验项目"

        sources = client.get("/api/future-radar/sources", headers=bearer).json()["items"]
        assert "PRIVATE ARTICLE TITLE" not in str(sources)
        assert all("latest_article_title" not in item for item in sources)
        assert all("latest_article_at" not in item for item in sources)
        dashboard = client.get(
            "/api/future-radar/dashboard", headers=bearer
        ).json()
        assert dashboard["counts"]["verified"] == 2
        assert dashboard["counts"]["pending"] == 0


def test_user_manual_scan_always_bridges_current_verified_legacy_pool(
    radar_service, monkeypatch
):
    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)
    monkeypatch.setattr(radar_service.repository, "due_sources", lambda: [])
    captured: dict = {}

    def capture_run(**kwargs):
        captured.update(kwargs)
        return {"id": "manual-bridge", "status": "success", "trigger_type": kwargs["trigger_type"]}

    monkeypatch.setattr(radar_service, "run", capture_run)
    with TestClient(main.app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-legacy-bridge-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        ).json()
        response = client.post(
            "/api/future-radar/run",
            headers={"Authorization": f"Bearer {registered['access_token']}"},
        )
        assert response.status_code == 200
    assert "legacy-recruitment-pipeline" in captured["source_ids"]
    assert len(captured["source_ids"]) > 1
    assert all(
        source.get("adapter_config", {}).get("adapter")
        in {"official_html", "legacy_database", "public_recruitment_index"}
        or (
            source.get("adapter_config", {}).get("adapter") == "official_api"
            and {
                "official-china-unicom-campus-2027": "china_unicom_campus",
                "official-china-telecom-campus-jobs-2027": "china_telecom_campus",
                "official-china-mobile-campus-notices": "china_mobile_notices",
            }.get(source["id"]) == source.get("adapter_config", {}).get("provider")
        )
        for source in radar_service.repository.user_scannable_sources()
    )
    assert captured["force"] is False


def test_due_sources_skip_unconfigured_discovery_placeholders(radar_service):
    due = radar_service.repository.due_sources()
    assert due
    assert all(
        source.get("adapter_config", {}).get("adapter") != "discovery_limited"
        for source in due
    )
    explicit = radar_service.repository.due_sources(
        source_ids=["wechat-guoyang-campus"]
    )
    assert [source["id"] for source in explicit] == ["wechat-guoyang-campus"]


def test_future_radar_startup_waits_for_first_upstream_refresh(
    radar_service, monkeypatch
):
    called: list[str] = []

    def capture_run(**_kwargs):
        called.append("radar")
        return {
            "id": "startup-order",
            "status": "success",
            "sources_succeeded": 0,
            "sources_checked": 0,
        }

    monkeypatch.setattr(main, "future_radar_service", radar_service)
    monkeypatch.setattr(radar_service, "run", capture_run)

    async def scenario():
        ready = asyncio.Event()
        task = asyncio.create_task(main.future_radar_refresh_loop(ready))
        await asyncio.sleep(0)
        assert called == []
        ready.set()
        for _ in range(20):
            if called:
                break
            await asyncio.sleep(0.01)
        assert called == ["radar"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_search_updates_pool_exposes_candidates_with_status_and_filters(
    radar_service, monkeypatch
):
    discovery = create_source(
        radar_service, "search-update-discovery", trust_level="discovery"
    )
    pending = {
        **sample_job("search-pending", title="搜索候选数据分析岗"),
        "company": "搜索候选银行",
        "primary_category": "internet_tech",
        "verification_status": "pending",
    }
    verified = {
        **sample_job("search-verified", title="官网已核验搜索岗位"),
        "company": "搜索核验科技",
        "primary_category": "internet_tech",
        "verification_status": "verified",
    }
    conflicted = {
        **sample_job("search-conflicted", title="日期冲突候选"),
        "company": "搜索冲突证券",
        "primary_category": "securities_public_funds_asset_management",
        "verification_status": "conflicted",
    }
    rejected = {
        **sample_job(
            "search-rejected", title="已关闭搜索候选", status="closed",
            verification_status="rejected",
        ),
        "company": "搜索关闭企业",
        "primary_category": "consumer_foreign_consulting",
    }
    radar_service.adapter_factory = lambda _source: StaticAdapter(AdapterResult(
        jobs=[pending, verified, conflicted, rejected],
        content_hash="search-update-candidates-v1",
        snapshot_complete=False,
        verified_job_external_ids={"search-verified"},
    ))
    assert radar_service.run(
        source_ids=[discovery["id"]], force=True
    )["new_jobs"] == 4

    official = create_source(
        radar_service, "official-only-source", trust_level="verification"
    )
    radar_service.adapter_factory = lambda _source: StaticAdapter(AdapterResult(
        jobs=[{
            **sample_job("official-only-job", verification_status="verified"),
            "company": "只在正式池企业",
            "primary_category": "internet_tech",
        }],
        content_hash="official-only-v1",
    ))
    assert radar_service.run(source_ids=[official["id"]], force=True)["new_jobs"] == 1

    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)

    with TestClient(main.app) as client:
        assert client.get("/api/future-radar/search-updates").status_code == 401
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-search-updates-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        )
        assert registered.status_code == 201
        bearer = {
            "Authorization": f"Bearer {registered.json()['access_token']}"
        }

        current_candidates = client.get(
            "/api/future-radar/search-updates", headers=bearer
        )
        assert current_candidates.status_code == 200
        assert current_candidates.json()["total"] == 3
        assert "search-rejected" not in {
            item["external_id"] for item in current_candidates.json()["items"]
        }

        candidates = client.get(
            "/api/future-radar/search-updates?status=all&page=1&page_size=10",
            headers=bearer,
        )
        assert candidates.status_code == 200
        body = candidates.json()
        assert body["pool"] == "search_updates"
        assert body["total"] == 4
        assert len(body["items"]) == len(body["candidates"]) == 4
        assert "只在正式池企业" not in {
            item["company"] for item in body["items"]
        }
        assert body["stats"]["verification_status"] == {
            "pending": 1,
            "verified": 1,
            "conflicted": 1,
            "rejected": 1,
        }
        assert body["stats"]["job_status"] == {
            "open": 3,
            "closed": 1,
            "unknown": 0,
        }
        assert body["stats"]["source_count"] == 1
        by_id = {item["external_id"]: item for item in body["items"]}
        assert by_id["search-pending"]["review_label"] == "待官网核验"
        assert by_id["search-pending"]["is_candidate"] is True
        assert by_id["search-pending"]["published_as_active_job"] is False
        assert all(
            "evidence" not in source
            for item in body["items"]
            for source in item["sources"]
        )
        assert by_id["search-verified"]["officially_verified"] is True
        assert by_id["search-verified"]["published_as_active_job"] is True

        filtered = client.get(
            "/api/future-radar/search-updates?status=open"
            "&verification_status=pending"
            "&category=internet_tech"
            "&source_id=search-update-discovery"
            "&q=%E6%90%9C%E7%B4%A2%E5%80%99%E9%80%89",
            headers=bearer,
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["external_id"] == "search-pending"
        assert filtered.json()["stats"]["total_candidates"] == 1

        detail = client.get(
            "/api/future-radar/search-updates/search-pending", headers=bearer
        )
        assert detail.status_code == 200
        assert detail.json()["review_state"] == "pending"
        assert client.get(
            "/api/future-radar/search-updates/official-only-job", headers=bearer
        ).status_code == 404
        assert client.get(
            "/api/future-radar/jobs/search-pending", headers=bearer
        ).status_code == 404

        official_jobs = client.get(
            "/api/future-radar/jobs?status=all", headers=bearer
        )
        assert official_jobs.status_code == 200
        assert {
            item["external_id"] for item in official_jobs.json()["items"]
        } == {"search-verified", "official-only-job"}
        assert client.get(
            "/api/future-radar/search-updates?category=unknown-sector",
            headers=bearer,
        ).status_code == 422


def test_future_radar_filters_by_one_primary_starfield_and_accepts_all_profile_categories(
    radar_service, monkeypatch
):
    source = create_source(radar_service, "structured-category-filter")
    public_fund = {
        **sample_job("category-public-fund", title="Investment Research Analyst"),
        "company": "示例公募基金",
        "employer_type": "公募基金",
        "industry": "资产管理",
        "primary_category": "securities_public_funds_asset_management",
        "organization_category": "public_fund",
        "industry_tags": ["asset_management", "public_fund"],
        "role_tags": ["investment_research"],
        "responsibilities": "负责基金投研、组合分析和行业研究。",
    }
    private_fund = {
        **sample_job("category-private-fund", title="Quantitative Researcher"),
        "company": "示例量化私募",
        "employer_type": "私募证券",
        "industry": "资产管理",
        "primary_category": "quant_private_hedge",
        "organization_category": "private_fund",
        "industry_tags": ["asset_management", "quant", "private_fund"],
        "role_tags": ["quant_research"],
        "responsibilities": "负责系统化投资研究、量化模型与组合研究。",
    }
    professional_services = {
        **sample_job("category-professional-services", title="AI & Data Consulting"),
        "company": "示例专业服务机构",
        "employer_type": "四大/专业服务",
        "industry": "专业服务",
        "primary_category": "big_four_professional_services",
        "organization_category": "professional_services",
        "industry_tags": ["professional_services"],
        "role_tags": ["ai", "data_science", "technology_consulting"],
        "responsibilities": "负责人工智能、数据科学与技术咨询项目。",
    }
    radar_service.adapter_factory = lambda _source: StaticAdapter(
        AdapterResult(
            jobs=[public_fund, private_fund, professional_services],
            content_hash="structured-category-filter-v1",
        )
    )
    assert radar_service.run(source_ids=[source["id"]], force=True)["new_jobs"] == 3

    settings_values = vars(main.settings).copy()
    settings_values.update({
        "database_path": database.settings.database_path,
        "future_radar_enabled": False,
        "recruitment_refresh_minutes": 0,
    })
    monkeypatch.setattr(main, "settings", SimpleNamespace(**settings_values))
    monkeypatch.setattr(main, "future_radar_service", radar_service)

    with TestClient(main.app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "future-radar-category-user",
                "password": "correct-horse-123",
                "privacy_accepted": True,
            },
        )
        assert registered.status_code == 201
        bearer = {"Authorization": f"Bearer {registered.json()['access_token']}"}

        saved = client.put(
            "/api/recruitment/profile",
            headers=bearer,
            json={"employer_types": list(PRIMARY_CATEGORY_CODES)},
        )
        assert saved.status_code == 200
        assert saved.json()["employer_types"] == list(PRIMARY_CATEGORY_CODES)

        public_only = client.get(
            "/api/future-radar/jobs?status=all"
            "&category=securities_public_funds_asset_management",
            headers=bearer,
        )
        assert public_only.status_code == 200
        assert public_only.json()["total"] == 1
        assert [item["external_id"] for item in public_only.json()["items"]] == [
            "category-public-fund"
        ]

        two_starfields = client.get(
            "/api/future-radar/jobs?status=all"
            "&category=quant_private_hedge"
            "&category=big_four_professional_services",
            headers=bearer,
        )
        assert two_starfields.status_code == 200
        assert two_starfields.json()["total"] == 2
        assert {item["primary_category"] for item in two_starfields.json()["items"]} == {
            "quant_private_hedge",
            "big_four_professional_services",
        }

        invalid = client.get(
            "/api/future-radar/jobs?status=all&category=unknown-sector",
            headers=bearer,
        )
        assert invalid.status_code == 422


def test_manual_scan_source_families_ignore_scheduler_intervals(radar_service):
    quick = create_source(
        radar_service,
        "manual-quick-official",
        source_type="official_html",
        adapter_config={"adapter": "official_html", "ai_extract": True},
    )
    deep = create_source(
        radar_service,
        "manual-deep-openai",
        trust_level="discovery",
        source_type="openai_web_search",
        adapter_config={"adapter": "openai_web_search"},
    )
    radar_service.repository.patch_source(quick["id"], {"interval_minutes": 43_200})
    radar_service.repository.patch_source(deep["id"], {"interval_minutes": 43_200})
    radar_service.repository.update_source_success(
        quick["id"], content_hash="quick-already-checked"
    )
    radar_service.repository.update_source_success(
        deep["id"], content_hash="deep-already-checked"
    )

    quick_ids = {
        source["id"]
        for source in radar_service.repository.manual_scan_sources("quick")
    }
    deep_ids = {
        source["id"]
        for source in radar_service.repository.manual_scan_sources("deep")
    }
    assert quick["id"] in quick_ids
    assert deep["id"] not in quick_ids
    assert deep["id"] in deep_ids
    assert quick["id"] not in deep_ids
    assert radar_service.repository.deep_scan_retry_after() == 0


def test_manual_deep_scan_bypasses_optional_ai_extraction_cache(radar_service):
    source = create_source(
        radar_service,
        "manual-deep-wechat",
        trust_level="discovery",
        source_type="wechat_public",
        adapter_config={"adapter": "wechat_public", "ai_extract": True},
    )
    refresh_flags: list[bool] = []

    class RecordingAdapter:
        def scan(self, current_source):
            refresh_flags.append(bool(
                current_source.get("adapter_config", {}).get("_force_refresh")
            ))
            return AdapterResult(content_hash=f"deep-refresh-{len(refresh_flags)}")

    radar_service.adapter_factory = lambda _source: RecordingAdapter()
    deep = radar_service.run(
        trigger_type="manual_deep",
        scan_type="deep",
        source_ids=[source["id"]],
    )
    scheduled = radar_service.run(
        trigger_type="scheduled-test",
        scan_type="scheduled",
        source_ids=[source["id"]],
    )

    assert deep["status"] == "success"
    assert scheduled["status"] == "success"
    assert refresh_flags == [True, False]


def test_database_run_lock_blocks_duplicate_after_refresh_and_releases_immediately(
    radar_service,
):
    import threading

    from backend.future_radar.service import RadarRunBusy

    source = create_source(
        radar_service,
        "run-lock-official",
        source_type="official_html",
        adapter_config={"adapter": "official_html", "ai_extract": False},
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    outcomes: list[dict] = []
    failures: list[Exception] = []

    class BlockingAdapter:
        def scan(self, current_source):
            calls.append(current_source["id"])
            entered.set()
            assert release.wait(timeout=5)
            return AdapterResult(content_hash="blocking-quick")

    radar_service.adapter_factory = lambda _source: BlockingAdapter()

    def run_first():
        try:
            outcomes.append(radar_service.run(
                trigger_type="manual_user",
                scan_type="quick",
                source_ids=[source["id"]],
            ))
        except Exception as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    thread = threading.Thread(target=run_first, daemon=True)
    thread.start()
    assert entered.wait(timeout=5)
    assert radar_service.repository.active_run_types() == ["quick"]

    # A new service represents a page refresh, browser, or second web worker.
    refreshed_service = FutureRadarService(
        connect=radar_service.repository._connect,
        openai_api_key="test-key",
        ai_model="test-radar-model",
        web_search_enabled=False,
    )
    with pytest.raises(RadarRunBusy) as busy:
        refreshed_service.run(
            trigger_type="manual_user",
            scan_type="quick",
            source_ids=[source["id"]],
        )
    assert busy.value.scan_type == "quick"
    assert busy.value.lock_type == "run"
    assert table_count(radar_service, "radar_runs") == 1
    assert calls == [source["id"]]

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    assert outcomes[0]["status"] == "success"
    assert radar_service.repository.active_run_types() == []

    radar_service.adapter_factory = lambda _source: StaticAdapter(
        AdapterResult(content_hash="immediate-second-quick")
    )
    second = radar_service.run(
        trigger_type="manual_user",
        scan_type="quick",
        source_ids=[source["id"]],
    )
    assert second["status"] == "success"
    assert table_count(radar_service, "radar_runs") == 2


def test_busy_source_is_skipped_while_other_source_continues(radar_service):
    busy_source = create_source(
        radar_service,
        "source-lock-busy",
        source_type="official_html",
        adapter_config={"adapter": "official_html", "ai_extract": False},
    )
    free_source = create_source(
        radar_service,
        "source-lock-free",
        source_type="official_html",
        adapter_config={"adapter": "official_html", "ai_extract": False},
    )
    calls: list[str] = []

    class CountingAdapter:
        def scan(self, source):
            calls.append(source["id"])
            return AdapterResult(content_hash=f"checked:{source['id']}")

    radar_service.adapter_factory = lambda _source: CountingAdapter()
    lock_name = f"future-radar-source:{busy_source['id']}"
    assert radar_service.repository.acquire_lock(
        lock_name, "other-run-owner", ttl_seconds=60
    )
    try:
        result = radar_service.run(
            trigger_type="manual_user",
            scan_type="quick",
            source_ids=[busy_source["id"], free_source["id"]],
        )
    finally:
        radar_service.repository.release_lock(lock_name, "other-run-owner")

    assert result["status"] == "partial_success"
    assert result["sources_checked"] == 2
    assert result["sources_succeeded"] == 1
    assert result["sources_failed"] == 0
    assert result["sources_skipped"] == 1
    assert result["errors"] == [{
        "source_id": busy_source["id"],
        "code": "SOURCE_BUSY",
        "message": "该信源已有扫描任务正在运行，本轮已跳过。",
    }]
    assert calls == [free_source["id"]]


def test_concurrent_deep_clicks_do_not_duplicate_openai_call(radar_service):
    import threading

    from backend.future_radar.service import RadarRunBusy

    source = create_source(
        radar_service,
        "deep-openai-lock",
        trust_level="discovery",
        source_type="openai_web_search",
        adapter_config={"adapter": "openai_web_search"},
    )
    entered = threading.Event()
    release = threading.Event()
    ai_calls: list[str] = []
    first_result: list[dict] = []

    class BlockingOpenAIAdapter:
        def scan(self, current_source):
            ai_calls.append(current_source["id"])
            entered.set()
            assert release.wait(timeout=5)
            return AdapterResult(
                content_hash="deep-discovery-result",
                ai_calls=1,
            )

    radar_service.adapter_factory = lambda _source: BlockingOpenAIAdapter()
    thread = threading.Thread(
        target=lambda: first_result.append(radar_service.run(
            trigger_type="manual_user",
            scan_type="deep",
            source_ids=[source["id"]],
        )),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(RadarRunBusy):
        radar_service.run(
            trigger_type="manual_user",
            scan_type="deep",
            source_ids=[source["id"]],
        )
    assert ai_calls == [source["id"]]
    assert table_count(radar_service, "radar_runs") == 1
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_result[0]["ai_calls"] == 1


def test_orchestration_exception_finalizes_created_run(radar_service, monkeypatch):
    source = create_source(
        radar_service,
        "orchestration-failure-official",
        source_type="official_html",
        adapter_config={"adapter": "official_html", "ai_extract": False},
    )

    class BrokenExecutor:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("executor failed before submission")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "backend.future_radar.service.ThreadPoolExecutor", BrokenExecutor
    )
    with pytest.raises(RuntimeError, match="executor failed"):
        radar_service.run(
            trigger_type="manual_user",
            scan_type="quick",
            source_ids=[source["id"]],
        )
    latest = radar_service.repository.list_runs(page_size=1)["items"][0]
    assert latest["status"] == "failed"
    assert latest["finished_at"] is not None
    assert latest["errors"][0]["code"] == "RUN_FAILED"
    assert radar_service.repository.active_run_types() == []


def test_force_refresh_bypasses_ai_cache_without_disabling_future_cache(radar_service):
    from backend.future_radar.ai import extract_recruitment_content

    calls: list[dict] = []
    response = SimpleNamespace(
        output_text=(
            '{"is_recruitment":false,"programs":[],"jobs":[]}'
        ),
        model="test-radar-model",
        usage=SimpleNamespace(input_tokens=12, output_tokens=4),
    )

    class FakeResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return response

    client = SimpleNamespace(responses=FakeResponses())
    arguments = {
        "repository": radar_service.repository,
        "content": "A deterministic public recruitment page.",
        "content_hash": "same-content-hash",
        "source_url": "https://example.com/campus",
        "model": "test-radar-model",
        "api_key": "test-key",
        "client": client,
    }

    first = extract_recruitment_content(**arguments)
    cached = extract_recruitment_content(**arguments)
    forced = extract_recruitment_content(**arguments, force_refresh=True)
    cached_after_force = extract_recruitment_content(**arguments)

    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert forced["cache_hit"] is False
    assert cached_after_force["cache_hit"] is True
    assert len(calls) == 2


def test_run_and_source_leases_renew_beyond_initial_ttl(radar_service):
    import threading
    import time
    from datetime import datetime, timezone

    from backend.future_radar.service import RadarRunBusy

    source = create_source(
        radar_service,
        "renewed-lease-official",
        source_type="official_html",
        adapter_config={"adapter": "official_html", "ai_extract": False},
    )
    service = FutureRadarService(
        connect=radar_service.repository._connect,
        openai_api_key="test-key",
        ai_model="test-radar-model",
        web_search_enabled=False,
        run_lock_ttl_seconds=1,
        source_lock_ttl_seconds=1,
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    outcomes: list[dict] = []
    failures: list[Exception] = []

    class BlockingAdapter:
        def scan(self, current_source):
            calls.append(current_source["id"])
            entered.set()
            assert release.wait(timeout=8)
            return AdapterResult(content_hash="renewed-lease-result")

    service.adapter_factory = lambda _source: BlockingAdapter()

    def first_run():
        try:
            outcomes.append(service.run(
                trigger_type="manual_quick",
                scan_type="quick",
                source_ids=[source["id"]],
            ))
        except Exception as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    thread = threading.Thread(target=first_run, daemon=True)
    thread.start()
    assert entered.wait(timeout=5)

    # Cross the original one-second expiry. Both heartbeats must have renewed
    # their leases rather than relying on the initial TTL.
    time.sleep(1.4)
    assert radar_service.repository.active_run_types() == ["quick"]
    with radar_service.repository._connect() as connection:
        leases = {
            row["lock_name"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM radar_locks WHERE lock_name IN (?, ?)",
                (
                    "future-radar-run:quick",
                    f"future-radar-source:{source['id']}",
                ),
            ).fetchall()
        }
    now = datetime.now(timezone.utc)
    assert datetime.fromisoformat(
        leases["future-radar-run:quick"]["expires_at"]
    ) > now
    assert datetime.fromisoformat(
        leases[f"future-radar-source:{source['id']}"]["expires_at"]
    ) > now

    # The renewed run lease still rejects an identical scan after its initial
    # TTL, and the renewed source lease makes another run skip only that source.
    contender = FutureRadarService(
        connect=radar_service.repository._connect,
        openai_api_key="test-key",
        ai_model="test-radar-model",
        web_search_enabled=False,
        run_lock_ttl_seconds=1,
        source_lock_ttl_seconds=1,
        adapter_factory=lambda _source: BlockingAdapter(),
    )
    with pytest.raises(RadarRunBusy):
        contender.run(
            trigger_type="manual_quick",
            scan_type="quick",
            source_ids=[source["id"]],
        )
    scheduled = contender.run(
        trigger_type="scheduled-test",
        scan_type="scheduled",
        source_ids=[source["id"]],
    )
    assert scheduled["sources_skipped"] == 1
    assert calls == [source["id"]]

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    assert outcomes[0]["status"] == "success"
    assert radar_service.repository.active_run_types() == []
