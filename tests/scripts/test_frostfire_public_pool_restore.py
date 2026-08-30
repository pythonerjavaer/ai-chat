"""Offline public-pool restoration tests: no accounts, secrets, or remote calls."""

from __future__ import annotations

import contextlib
import copy
from collections import Counter
from datetime import date
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "frostfire_public_pool_restore_tests_module",
    ROOT / "scripts" / "frostfire_public_pool_restore.py",
)
assert SPEC and SPEC.loader
restore = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = restore
SPEC.loader.exec_module(restore)

STAMP = "2026-08-30T12:00:00+00:00"
PERSONAL_DERIVED = "fixture-only-personal-match-reason-not-to-copy"


def make_job(index: int, *, status: str = "open", verification: str = "pending"):
    year = date.today().year + int(date.today().month >= 6)
    return {
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "external_id": f"fixture-job-{index}",
        "program_id": None, "program_name": None, "recruitment_year": None,
        "company": "示例科技有限公司", "title": f"{year}届校园招聘算法工程师{index}",
        "city": "上海", "region": "中国大陆", "employer_type": "互联网企业",
        "industry": "软件", "primary_category": "internet_tech",
        "organization_category": "", "industry_tags": ["software"], "role_tags": ["engineering"],
        "official_url": f"https://careers.example.com/job/{index}#public-position",
        "application_url": f"https://careers.example.com/apply?id={index}#application",
        "opening_date": "2026-08-31" if status == "unknown" else None,
        "closing_date": None, "status": status, "verification_status": verification,
        "confidence_score": 0.95 if verification == "verified" else 0.5,
        "description": "公开招聘说明", "responsibilities": "", "requirements": "面向应届毕业生",
        "tags": ["校园招聘"], "first_seen_at": STAMP, "last_seen_at": STAMP,
        "last_changed_at": STAMP, "latest_event_type": "NEW", "latest_event_at": STAMP,
        "sources": [{
            "source_id": "legacy-search-discovery", "name": "历史搜索与同步候选",
            "source_type": "manual", "trust_level": "discovery",
            "source_url": f"https://careers.example.com/job/{index}#public-position",
            "verification_role": "discovery", "discovered_at": STAMP, "last_seen_at": STAMP,
            "active": True,
        }],
    }


def make_payload(jobs=None):
    jobs = jobs if jobs is not None else [make_job(1, verification="verified"), make_job(2, status="unknown")]
    return {
        "schema_version": restore.VERSION, "captured_at": STAMP,
        "source_origin": "https://frostfire-ai.onrender.com/",
        "counts": restore._counts(jobs), "jobs": jobs,
    }


class PublicPoolRestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="frostfire-public-restore-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database_path = self.root / "isolated.sqlite3"
        self.counter = 0
        self.network = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def snapshot(self, payload=None):
        self.counter += 1
        path = self.root / f"public-fixture-{self.counter}.json"
        raw = json.dumps(payload if payload is not None else make_payload(), ensure_ascii=False).encode()
        path.write_bytes(raw)
        return restore.load_snapshot(path, hashlib.sha256(raw).hexdigest())

    def repository(self):
        # Only pure recruitment logic is loaded. Never import app settings or
        # use the application's configured database for an offline fixture.
        stub = types.ModuleType("backend.config")
        stub.settings = types.SimpleNamespace()
        with mock.patch.dict(sys.modules, {"backend.config": stub}):
            from backend.future_radar.repository import RadarRepository
        return RadarRepository(self.connect)

    def test_offline_cli_does_not_read_target_credentials(self):
        self.snapshot()
        path = self.root / "public-fixture-1.json"
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        output = io.StringIO()
        original_get = restore.os.environ.get

        def no_target_credentials(key, *args):
            if key == "FROSTFIRE_PUBLIC_POOL_DATABASE_URL":
                raise AssertionError("offline validation must not read target credentials")
            return original_get(key, *args)

        with mock.patch.object(restore.os.environ, "get", side_effect=no_target_credentials), contextlib.redirect_stdout(output):
            code = restore.main(["--snapshot", str(path), "--expected-sha256", sha])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "dry_run_validated_no_remote_connection")
        self.assertFalse(self.database_path.exists())

    def test_hash_and_declared_counts_are_required(self):
        snapshot = self.snapshot()
        path = self.root / "public-fixture-1.json"
        with self.assertRaisesRegex(restore.RestoreError, "sha256_mismatch"):
            restore.load_snapshot(path, "0" * 64)
        for mutation in ("total", "status", "duplicate_id", "duplicate_external"):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(snapshot.data)
                if mutation == "total":
                    data["counts"]["total"] += 1
                elif mutation == "status":
                    data["counts"]["status"]["open"] += 1
                elif mutation == "duplicate_id":
                    data["jobs"][1]["id"] = data["jobs"][0]["id"]
                else:
                    data["jobs"][1]["external_id"] = data["jobs"][0]["external_id"]
                with self.assertRaises(restore.RestoreError):
                    self.snapshot(data)

    def test_private_transport_and_credentials_are_rejected(self):
        mutations = [
            lambda job: job.update(source_thread_id="not-a-real-thread"),
            lambda job: job.update(official_url="https://chatgpt.com/c/not-a-real-conversation"),
            lambda job: job.update(requirements="fixture@example.invalid"),
            lambda job: job.update(requirements="sk-" + "x" * 24),
            lambda job: job.update(application_url="https://example.com/?access_token=fixture"),
            lambda job: job.update(official_url="http://careers.example.com/job"),
            lambda job: job.update(official_url="https://127.0.0.1/private"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                job = make_job(1)
                mutate(job)
                with self.assertRaises(restore.RestoreError):
                    self.snapshot(make_payload([job]))

    def test_public_ats_identifiers_and_fragment_urls_are_preserved(self):
        job = make_job(1)
        job["external_id"] = "ats-13800138000"
        job["application_url"] = "https://careers.example.com/job/13800138000#apply"
        snapshot = self.snapshot(make_payload([job]))
        self.assertEqual(snapshot.jobs[0]["external_id"], job["external_id"])
        self.assertEqual(snapshot.jobs[0]["application_url"], job["application_url"])
        job["external_id"] = "ats-[redacted-phone]-position"
        self.assertEqual(self.snapshot(make_payload([job])).jobs[0]["external_id"], job["external_id"])

    def test_restore_preserves_job_facts_status_dates_ids_and_provenance(self):
        data = make_payload()
        data["jobs"][0]["match_reasons"] = [PERSONAL_DERIVED]
        duplicate_source = {**data["jobs"][0]["sources"][0], "source_url": "https://careers.example.com/alternate"}
        data["jobs"][0]["sources"].append(duplicate_source)
        snapshot = self.snapshot(data)
        result = restore.apply_snapshot(snapshot, self.connect)
        self.assertEqual(result["counts"], data["counts"])
        self.assertFalse(result["idempotent_replay"])
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM radar_jobs ORDER BY id")]
            self.assertEqual([row["id"] for row in rows], [job["id"] for job in snapshot.jobs])
            self.assertEqual([row["external_id"] for row in rows], [job["external_id"] for job in snapshot.jobs])
            self.assertEqual([row["verification_status"] for row in rows], ["verified", "pending"])
            self.assertEqual(rows[1]["status"], "unknown")
            self.assertEqual(rows[1]["opening_date"], "2026-08-31")
            self.assertIsNone(rows[1]["closing_date"])
            self.assertEqual(rows[0]["application_url"], data["jobs"][0]["application_url"])
            blob = connection.execute("SELECT metadata FROM radar_source_snapshots").fetchone()[0]
            self.assertNotIn(PERSONAL_DERIVED, blob)
            self.assertEqual(json.loads(blob)["snapshot"]["jobs"][0]["sources"], snapshot.jobs[0]["sources"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0], 4)
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
        repository = self.repository()
        active = repository.list_opportunities(filters={"status": "active"}, public_url=restore._url, prepare=dict)
        only_open = repository.list_opportunities(filters={"status": "open"}, public_url=restore._url, prepare=dict)
        self.assertEqual(active["total"], 2)
        self.assertEqual(active["stats"]["verified_count"], 1)
        self.assertEqual(active["stats"]["discovered_count"], 1)
        self.assertEqual(only_open["total"], 1)
        self.assertIsNotNone(repository.get_opportunity(snapshot.jobs[1]["id"], public_url=restore._url))

    def test_same_snapshot_is_idempotent_and_different_snapshot_cannot_overwrite(self):
        snapshot = self.snapshot()
        restore.apply_snapshot(snapshot, self.connect)
        repeated = restore.apply_snapshot(snapshot, self.connect)
        self.assertTrue(repeated["idempotent_replay"])
        different = make_payload()
        different["jobs"][0]["requirements"] = "另一份公开数据"
        with self.assertRaisesRegex(restore.RestoreError, "target_recruitment_data_not_empty"):
            restore.apply_snapshot(self.snapshot(different), self.connect)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM radar_sync_batches").fetchone()[0], 1)
            restore._verify(connection, snapshot)

    def test_normalized_company_aliases_share_entity_without_losing_job_names(self):
        jobs = [make_job(1), make_job(2)]
        jobs[0]["company"], jobs[1]["company"] = "UBS(瑞银)", "UBS 瑞银"
        snapshot = self.snapshot(make_payload(jobs))
        restore.apply_snapshot(snapshot, self.connect)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM radar_companies").fetchone()[0], 1)
            rows = connection.execute("SELECT company, company_id FROM radar_jobs ORDER BY id").fetchall()
            self.assertEqual([row["company"] for row in rows], ["UBS(瑞银)", "UBS 瑞银"])
            self.assertEqual(rows[0]["company_id"], rows[1]["company_id"])
            restore._verify(connection, snapshot)

    def test_company_and_program_tampering_prevents_idempotent_success(self):
        job = make_job(1)
        job.update(program_id="public-program-1", program_name="原有公开校招项目", recruitment_year=2027)
        snapshot = self.snapshot(make_payload([job]))
        restore.apply_snapshot(snapshot, self.connect)
        with self.connect() as connection:
            program = connection.execute("SELECT * FROM recruitment_programs").fetchone()
            self.assertEqual(program["status"], "unknown")
            self.assertEqual(program["verification_status"], "pending")
        for table, field, replacement in [
            ("radar_companies", "name", "错误公司名称"),
            ("recruitment_programs", "company", "错误项目公司"),
            ("recruitment_programs", "program_name", "错误项目名称"),
            ("recruitment_programs", "recruitment_year", 2028),
        ]:
            with self.subTest(table=table, field=field):
                with self.connect() as connection:
                    previous = connection.execute(f"SELECT {field} FROM {table}").fetchone()[0]
                    connection.execute(f"UPDATE {table} SET {field}=?", (replacement,))
                with self.assertRaisesRegex(restore.RestoreError, "registry_does_not_match"):
                    restore.apply_snapshot(snapshot, self.connect)
                with self.connect() as connection:
                    connection.execute(f"UPDATE {table} SET {field}=?", (previous,))

    def test_failure_rolls_back_rows_and_schema_in_same_transaction(self):
        snapshot = self.snapshot()
        original = restore._insert
        def failing_insert(connection, table, row):
            if table == "job_sources":
                raise restore.RestoreError("fixture_failure")
            return original(connection, table, row)
        with mock.patch.object(restore, "_insert", side_effect=failing_insert):
            with self.assertRaisesRegex(restore.RestoreError, "fixture_failure"):
                restore.apply_snapshot(snapshot, self.connect)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)

    def test_account_tables_are_never_accessed(self):
        with self.connect() as connection:
            connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, password_hash TEXT)")
            connection.execute("INSERT INTO users VALUES (1,'fixture-only-password-hash')")
        def isolated_connect():
            connection = self.connect()
            def authorize(action, first, second, database, trigger):
                if first == "users" and action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            return connection
        restore.apply_snapshot(self.snapshot(), isolated_connect)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT password_hash FROM users").fetchone()[0], "fixture-only-password-hash")

    def test_snapshot_anchor_survives_registry_seed_and_empty_legacy_scan(self):
        snapshot = self.snapshot()
        restore.apply_snapshot(snapshot, self.connect)
        repository = self.repository()
        from backend.future_radar.seeds import initial_sources
        repository.seed_sources(initial_sources(web_search_enabled=False))
        source = repository.get_source("legacy-search-discovery")
        with repository.transaction() as connection:
            closed = repository.process_missing_jobs(
                connection, source=source, seen_job_ids=set(), threshold=1,
                run_id="offline-empty-legacy-scan", now=STAMP,
            )
        self.assertEqual(closed, 0)
        source = repository.get_source(snapshot.anchor)
        self.assertFalse(source["enabled"])
        self.assertEqual(source["adapter_config"], {"adapter": "manual"})
        active = repository.list_opportunities(filters={"status": "active"}, public_url=restore._url, prepare=dict)
        self.assertEqual(active["total"], 2)
        self.assertEqual(active["stats"]["job_status"]["unknown"], 1)

    def test_cli_database_error_never_prints_dsn(self):
        snapshot = self.snapshot()
        storage = types.ModuleType("backend.storage")
        sensitive = "postgresql://fixture_user:fixture_password@db.example.invalid/database"
        storage.connect_postgres = mock.Mock(side_effect=RuntimeError(sensitive))
        storage.close_postgres_pools = mock.Mock()
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"backend.storage": storage}), mock.patch.dict(os.environ, {"FROSTFIRE_PUBLIC_POOL_DATABASE_URL": sensitive}), contextlib.redirect_stderr(output):
            code = restore.main(["--snapshot", str(self.root / "public-fixture-1.json"), "--expected-sha256", snapshot.sha256, "--apply"])
        self.assertEqual(code, 2)
        self.assertNotIn("fixture_password", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "public_pool_restore_failed")
        storage.close_postgres_pools.assert_called_once()

    @unittest.skipUnless(os.environ.get("FROSTFIRE_PUBLIC_POOL_TEST_SNAPSHOT"), "explicit public snapshot fixture not supplied")
    def test_explicit_public_snapshot_restores_all_active_opportunities(self):
        snapshot = restore.load_snapshot(
            Path(os.environ["FROSTFIRE_PUBLIC_POOL_TEST_SNAPSHOT"]),
            os.environ["FROSTFIRE_PUBLIC_POOL_TEST_SHA256"],
        )
        result = restore.apply_snapshot(snapshot, self.connect)
        self.assertEqual(result["counts"], snapshot.data["counts"])
        repository = self.repository()
        active = repository.list_opportunities(filters={"status": "active"}, public_url=restore._url, prepare=dict, page_size=5_000)
        self.assertEqual(active["total"], snapshot.data["counts"]["total"])
        self.assertEqual(active["stats"]["verified_count"], snapshot.data["counts"]["verification_status"]["verified"])
        self.assertEqual(active["stats"]["discovered_count"], snapshot.data["counts"]["verification_status"]["pending"] + snapshot.data["counts"]["verification_status"]["conflicted"])
        self.assertEqual(len({row["id"] for row in active["items"]}), snapshot.data["counts"]["total"])
        self.assertTrue(all(row["official_url"] or row["application_url"] for row in active["items"]))
        self.assertEqual(active["stats"]["category_counts"], dict(Counter(row["primary_category"] or "uncategorized" for row in active["items"])))
        self.assertTrue(restore.apply_snapshot(snapshot, self.connect)["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
