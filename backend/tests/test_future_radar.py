import os
import sqlite3
from copy import deepcopy
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
    WechatSourceAdapter,
)
from backend.future_radar.normalization import (
    PRIMARY_CATEGORY_CODES,
    normalize_job,
)
from backend.future_radar.schema import migrate
from backend.future_radar.schemas import FrostFireSyncV1, RadarJobInput
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
    try:
        migrate(connection)
        assert connection.execute(
            "SELECT primary_category FROM radar_jobs WHERE id='legacy-tag-row'"
        ).fetchone()[0] == "quant_private_hedge"
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


def test_openai_adapter_failure_degrades_without_stopping_deterministic_source(radar_service):
    ai_source = create_source(
        radar_service,
        "openai-test-source",
        trust_level="discovery",
        source_type="openai_web_search",
    )
    deterministic = create_source(radar_service, "deterministic-test-source")
    adapters = {
        ai_source["id"]: FailingAdapter("OpenAI is unavailable"),
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


def test_future_radar_api_paginates_requires_admin_run_and_strict_sync(
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

    with TestClient(main.app) as client:
        unauthorized = client.post(
            "/api/future-radar/run",
            json={"source_ids": ["mock-future-radar"], "force": True},
        )
        assert unauthorized.status_code == 401

        manual = client.post(
            "/api/future-radar/run",
            headers={"X-Admin-Token": "test-admin-dashboard-token"},
            json={"source_ids": ["mock-future-radar"], "force": True},
        )
        assert manual.status_code == 200
        assert manual.json()["new_jobs"] == 12

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
