"""Isolated full-schema migration tests; never use app/production credentials."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("frostfire_database_migrate_tests_module", ROOT / "scripts" / "frostfire_database_migrate.py")
assert SPEC and SPEC.loader
migrate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migrate
SPEC.loader.exec_module(migrate)

FIXTURE_PRIVATE_TEXT = "fixture-only-private-document-🧊-do-not-print"
FIXTURE_PASSWORD_HASH = "$argon2id$fixture-only-not-a-production-password-hash"


def _literal_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left), _literal_string(node.right)
        return left + right if left is not None and right is not None else None
    return None


def create_complete_fixture(path: Path, *, populated: bool = True) -> sqlite3.Connection:
    """Extract trusted checked-in schema literals, without importing app/config."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    for filename in (ROOT / "backend" / "database.py", ROOT / "backend" / "future_radar" / "schema.py"):
        tree = ast.parse(filename.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args]
        for node in calls:
            sql = _literal_string(node.args[0])
            if node.func.attr == "executescript" and sql and "CREATE TABLE" in sql:
                connection.executescript(sql)
        for node in calls:
            sql = _literal_string(node.args[0])
            if node.func.attr == "execute" and sql and sql.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
                connection.execute(sql)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "_ensure_column" or len(node.args) != 4:
                continue
            table, column, declaration = (_literal_string(arg) for arg in node.args[1:])
            if table and column and declaration:
                existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({migrate.identifier(table)})")}
                if column not in existing:
                    connection.execute(f"ALTER TABLE {migrate.identifier(table)} ADD COLUMN {migrate.identifier(column)} {declaration}")
    if "plan" not in {row["name"] for row in connection.execute("PRAGMA table_info(users)")}:
        connection.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
    connection.execute("PRAGMA foreign_keys=ON")
    if populated:
        schema = migrate.inspect_schema(connection)
        records = {}
        for table in schema:
            record = {}
            for column in table.columns:
                if column.default is not None:
                    record[column.name] = connection.execute("SELECT " + column.default).fetchone()[0]
                elif not column.not_null and not column.pk_order:
                    record[column.name] = None
                elif column.kind == "INTEGER":
                    record[column.name] = 9_007_199_254_740_993 if column.pk_order else 1
                elif column.kind == "REAL":
                    record[column.name] = 0.12345678987654321
                else:
                    record[column.name] = f"fixture-{table.name}-{column.name}"
            if table.name == "users":
                record.update(username="MigrationFixture@example.invalid", password_hash=FIXTURE_PASSWORD_HASH)
            if table.name == "messages":
                record.update(role="user", content=FIXTURE_PRIVATE_TEXT)
            if table.name == "documents":
                record.update(content=FIXTURE_PRIVATE_TEXT)
            if table.name == "chunks":
                record.update(embedding="[0.12345678987654321, -1, 0]", content=FIXTURE_PRIVATE_TEXT)
            if table.name in {"radar_jobs", "recruitment_programs"}:
                record.update(confidence_score=0.12345678987654321)
            if table.name == "radar_jobs":
                record.update(primary_category="internet")
            if table.name == "recruitment_watches":
                record.update(fetch_url=record["url"])
            if table.name == "schema_migrations":
                record.update(version="future_radar_v1", applied_at="2026-08-30T00:00:00Z")
            for foreign_key in table.foreign_keys:
                referenced = record if foreign_key.target == table.name else records[foreign_key.target]
                for col, ref_col in zip(foreign_key.columns, foreign_key.target_columns):
                    record[col] = referenced[ref_col]
            records[table.name] = record
            cols = ",".join(migrate.identifier(column.name) for column in table.columns)
            placeholders = ",".join("?" for _ in table.columns)
            connection.execute(f"INSERT INTO {migrate.identifier(table.name)} ({cols}) VALUES ({placeholders})", tuple(record[column.name] for column in table.columns))
        # Preserve AUTOINCREMENT high-water even when the high-ID row was deleted.
        connection.execute("UPDATE sqlite_sequence SET seq=seq+100 WHERE name='users'")
        connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", ("future_radar_v2_job_taxonomy", "2026-08-30T00:00:00Z"))
        connection.commit()
        migrate.check_sqlite(connection)
    return connection


class LocalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ff-migrate-unit-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.sqlite3"
        self.connection = create_complete_fixture(self.source)
        self.addCleanup(self.connection.close)

    def test_default_dry_run_is_full_private_backup_without_secrets_or_network(self):
        output, errors = io.StringIO(), io.StringIO()
        before = self.source.read_bytes()
        with mock.patch.object(migrate, "_connect_postgres") as target, mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = migrate.main(["--source", str(self.source), "--backup-dir", str(self.root / "backup")])
        self.assertEqual(code, 0, errors.getvalue())
        target.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["application_table_count"], 30)
        self.assertEqual(report["foreign_key_count"], 29)
        self.assertEqual(report["synthetic_user_count"], 1)
        self.assertEqual(sum(row["rows"] for row in report["tables"].values()), 31)
        snapshot = Path(report["snapshot"])
        self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.with_suffix(".manifest.json").stat().st_mode), 0o600)
        self.assertEqual(self.source.read_bytes(), before)
        for private in (FIXTURE_PRIVATE_TEXT, FIXTURE_PASSWORD_HASH, "MigrationFixture@example.invalid"):
            self.assertNotIn(private, output.getvalue() + errors.getvalue())

    def test_import_needs_no_openai_config_or_driver(self):
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/frostfire_database_migrate.py"), "--source", str(self.source), "--backup-dir", str(self.root / "stdlib-only")],
            env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "dry_run_verified_no_remote_connection")

    def test_read_connection_rejects_source_mutation(self):
        readonly = migrate.read_sqlite(self.source)
        self.addCleanup(readonly.close)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.execute("DELETE FROM users")

    def test_online_backup_includes_committed_wal_and_excludes_uncommitted_write(self):
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.execute("UPDATE system_state SET value='committed-wal'")
        self.connection.commit()
        self.connection.execute("UPDATE system_state SET value='not-committed'")
        snapshot = migrate.backup_sqlite(self.source, self.root / "wal-backup")
        copied = migrate.read_sqlite(snapshot)
        self.addCleanup(copied.close)
        self.assertEqual(copied.execute("SELECT value FROM system_state").fetchone()[0], "committed-wal")
        self.connection.rollback()

    def test_backup_rejects_insecure_directory_and_symlink_source(self):
        insecure = self.root / "public-backup"
        insecure.mkdir(mode=0o755)
        insecure.chmod(0o755)
        with self.assertRaisesRegex(migrate.MigrationError, "0700"):
            migrate.backup_sqlite(self.source, insecure)
        link = self.root / "linked.sqlite3"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(migrate.MigrationError, "regular_sqlite_file"):
            migrate.backup_sqlite(link, self.root / "safe")

    def test_foreign_key_corruption_is_rejected_before_remote_connect(self):
        self.connection.execute("PRAGMA foreign_keys=OFF")
        self.connection.execute("UPDATE sessions SET user_id=19")
        self.connection.commit()
        with self.assertRaisesRegex(migrate.MigrationError, "foreign_key_check_failed"):
            migrate.backup_sqlite(self.source, self.root / "bad-fk")

    def test_unknown_tables_or_trigger_are_fail_closed(self):
        self.connection.execute("CREATE TABLE unknown_data (id INTEGER)")
        with self.assertRaisesRegex(migrate.MigrationError, "30_audited"):
            migrate.inspect_schema(self.connection)
        self.connection.execute("DROP TABLE unknown_data")
        self.connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON system_state BEGIN SELECT 1; END")
        with self.assertRaisesRegex(migrate.MigrationError, "views_or_triggers"):
            migrate.inspect_schema(self.connection)

    def test_audited_cache_triggers_are_accepted_without_loading_application(self):
        contract = migrate._revision_contract()
        contract.install_opportunity_revision(self.connection)
        self.connection.commit()
        self.assertEqual(len(migrate.inspect_schema(self.connection)), 30)
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "scripts/frostfire_database_migrate.py"),
             "--source", str(self.source), "--backup-dir", str(self.root / "with-cache")],
            env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "dry_run_verified_no_remote_connection")

    def test_same_cache_trigger_name_with_different_sql_is_rejected(self):
        contract = migrate._revision_contract()
        name, (table, _) = next(iter(contract.SQLITE_REVISION_TRIGGERS.items()))
        self.connection.execute(
            f'CREATE TRIGGER "{name}" AFTER INSERT ON "{table}" BEGIN SELECT 1; END'
        )
        with self.assertRaisesRegex(migrate.MigrationError, "views_or_triggers"):
            migrate.inspect_schema(self.connection)

    def test_expression_index_is_not_executed_from_untrusted_sql(self):
        self.connection.execute("CREATE INDEX unexpected_expression ON users (length(username))")
        with self.assertRaisesRegex(migrate.MigrationError, "expression_index"):
            migrate.inspect_schema(self.connection)

    def test_production_source_blocks_reserved_test_users_before_connection(self):
        tables = migrate.inspect_schema(self.connection)
        digests = migrate.database_digest(self.connection, tables)
        with mock.patch.object(migrate, "_connect_postgres") as target:
            with self.assertRaisesRegex(migrate.MigrationError, "synthetic_users"):
                migrate.apply_snapshot(self.source, tables, digests, source_kind="production")
        target.assert_not_called()

    def test_dsn_security_and_test_fixture_destination(self):
        rejected = [
            ("postgresql://user@example.com/db?sslmode=require", False),
            ("postgresql://user@example.com/db?sslmode=verify-full", False),
            ("postgresql://user@127.0.0.1/db?host=example.com", True),
            ("postgresql://user@example.com/db", True),
        ]
        for dsn, test_source in rejected:
            with self.subTest(test_source=test_source), self.assertRaises(migrate.MigrationError):
                migrate.validate_target_dsn(dsn, test_source=test_source, target_env="FROSTFIRE_TEST_POSTGRES_URL")
        migrate.validate_target_dsn("postgresql://user@127.0.0.1/db", test_source=True, target_env="FROSTFIRE_TEST_POSTGRES_URL")

    def test_errors_never_echo_unknown_argument_or_database_exception_values(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            migrate.main(["--dsn", "postgresql://user:DO_NOT_LOG@example.com/db"])
        self.assertNotIn("DO_NOT_LOG", errors.getvalue())
        with mock.patch.object(migrate, "backup_sqlite", side_effect=RuntimeError(FIXTURE_PASSWORD_HASH)), contextlib.redirect_stderr(errors):
            code = migrate.main(["--source", str(self.source)])
        self.assertEqual(code, 3)
        self.assertNotIn(FIXTURE_PASSWORD_HASH, errors.getvalue())

    def test_integer_and_real_preserve_full_sqlite_precision(self):
        tables = migrate.inspect_schema(self.connection)
        self.assertIn("BIGINT GENERATED BY DEFAULT AS IDENTITY", migrate.create_table_sql(tables[0]))
        radar = next(table for table in tables if table.name == "radar_jobs")
        self.assertIn("DOUBLE PRECISION", migrate.create_table_sql(radar))
        self.assertGreater(tables[0].high_water, 2**53)
        for value in ("datetime('now')", "(SELECT 1)", "'safe'; DROP TABLE users", str(2**100)):
            with self.assertRaises(migrate.MigrationError):
                migrate.literal_default(value)

    def test_sqlite_dynamic_type_and_nullable_text_pk_do_not_silently_coerce(self):
        tables = migrate.inspect_schema(self.connection)
        self.connection.execute("UPDATE recruitment_jobs SET historical_applicants=1.5")
        with self.assertRaisesRegex(migrate.MigrationError, "lossy_row_value"):
            migrate.database_digest(self.connection, tables)
        self.connection.execute("UPDATE recruitment_jobs SET historical_applicants=NULL")
        self.connection.execute("UPDATE system_state SET key=NULL")
        with self.assertRaisesRegex(migrate.MigrationError, "null_primary_key"):
            migrate.database_digest(self.connection, tables)


@unittest.skipUnless(os.getenv("FROSTFIRE_TEST_POSTGRES_URL"), "isolated loopback PostgreSQL not configured")
class PostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        # Safety check occurs before even importing or connecting the driver.
        self.dsn = os.environ["FROSTFIRE_TEST_POSTGRES_URL"]
        migrate.validate_target_dsn(self.dsn, test_source=True, target_env="FROSTFIRE_TEST_POSTGRES_URL")
        self.directory = tempfile.TemporaryDirectory(prefix="ff-migrate-pg-")
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "source.sqlite3"
        source = create_complete_fixture(self.source)
        source.close()
        self.snapshot = migrate.backup_sqlite(self.source, Path(self.directory.name) / "backup")
        self.sqlite = migrate.read_sqlite(self.snapshot)
        self.addCleanup(self.sqlite.close)
        self.tables = migrate.inspect_schema(self.sqlite)
        self.digests = migrate.database_digest(self.sqlite, self.tables)
        self.schema = "ff_migrate_test_" + uuid.uuid4().hex[:12]
        self.addCleanup(self._remove_only_owned_test_schema)

    def _remove_only_owned_test_schema(self):
        if not self.schema.startswith("ff_migrate_test_"):
            raise AssertionError("refusing cleanup outside owned isolated schema")
        with migrate._connect_postgres(self.dsn, self.schema) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {migrate.identifier(self.schema)} CASCADE")

    def apply(self):
        return migrate.apply_snapshot(self.snapshot, self.tables, self.digests, schema=self.schema, target_env="FROSTFIRE_TEST_POSTGRES_URL", source_kind="test")

    def test_complete_30_table_29_fk_round_trip_and_idempotent_repeat(self):
        self.assertEqual(self.apply(), "committed_and_verified")
        self.assertEqual(self.apply(), "identical_target_skipped")
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            self.assertEqual(migrate.database_digest(target, self.tables), self.digests)
            self.assertEqual(len({row["relname"] for row in migrate._target_objects(target, self.schema) if row["relkind"] == "r"}), 30)
            user = target.execute("SELECT id, password_hash FROM users").fetchone()
            self.assertEqual(user["id"], 9_007_199_254_740_993)
            self.assertEqual(user["password_hash"], FIXTURE_PASSWORD_HASH)
            self.assertEqual(target.execute("SELECT content FROM documents").fetchone()[0], FIXTURE_PRIVATE_TEXT)
            self.assertEqual(target.execute("SELECT embedding FROM chunks").fetchone()[0], "[0.12345678987654321, -1, 0]")
            self.assertEqual(target.execute("SELECT cached_from_run_id=id AS linked FROM space_runs").fetchone()[0], True)
            self.assertEqual(target.execute("SELECT COUNT(*) FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=? AND con.contype='f' AND con.condeferrable", (self.schema,)).fetchone()[0], 29)

    def test_migrated_database_survives_real_application_init_without_data_changes(self):
        self.apply()
        # Separate process prevents settings/module globals from leaking into
        # other tests. dotenv is patched before app config is imported.
        script = """
import os
from unittest.mock import patch
with patch('dotenv.load_dotenv'), patch.dict(os.environ, {
    'OPENAI_API_KEY': 'migration-fixture-never-called',
    'JWT_SECRET': 'migration-fixture-never-used-for-login',
    'DATABASE_BACKEND': 'sqlite',
}):
    from backend.database import init_db
from backend.storage import connect_postgres, close_postgres_pools
init_db(connection_factory=lambda: connect_postgres(
    os.environ['FROSTFIRE_TEST_POSTGRES_URL'],
    schema=os.environ['FROSTFIRE_TEST_SCHEMA'],
))
close_postgres_pools()
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            env={"PATH": os.environ.get("PATH", ""), "FROSTFIRE_TEST_POSTGRES_URL": self.dsn, "FROSTFIRE_TEST_SCHEMA": self.schema},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            actual = migrate.database_digest(target, self.tables)
            runtime_metadata = {"system_state", "schema_migrations"}
            self.assertEqual(
                {k: v for k, v in actual.items() if k not in runtime_metadata},
                {k: v for k, v in self.digests.items() if k not in runtime_metadata},
            )
            before_state = dict(self.sqlite.execute("SELECT key,value FROM system_state").fetchall())
            after_state = {row["key"]: row["value"] for row in target.execute("SELECT key,value FROM system_state").fetchall()}
            contract = migrate._revision_contract()
            self.assertEqual(set(after_state) - set(before_state), {contract.REVISION_KEY, contract.NAMESPACE_KEY})
            self.assertTrue(all(after_state[key] == value for key, value in before_state.items()))
            migrate.verify_target_revision_triggers(target, self.schema)
            before_migrations = dict(self.sqlite.execute("SELECT version,applied_at FROM schema_migrations").fetchall())
            after_migrations = {row["version"]: row["applied_at"] for row in target.execute("SELECT version,applied_at FROM schema_migrations").fetchall()}
            self.assertEqual(set(after_migrations) - set(before_migrations), {
                "future_radar_v3_operator_categories",
                "future_radar_v4_employer_directory_categories",
            })
            self.assertTrue(all(after_migrations[key] == value for key, value in before_migrations.items()))
            self.assertEqual(target.execute("SELECT id,password_hash FROM users").fetchone()["password_hash"], FIXTURE_PASSWORD_HASH)
            migrate.check_target_foreign_keys(target, self.tables)
        # Added runtime metadata is real drift: never silently hide it from a
        # full-database migration comparison, even though user rows are intact.
        with self.assertRaisesRegex(migrate.MigrationError, "different_data_no_changes"):
            self.apply()

    def test_postgres_audit_accepts_only_fixed_cache_trigger_and_function_definitions(self):
        self.apply()
        contract = migrate._revision_contract()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            contract.install_opportunity_revision(target)
            migrate.verify_target_schema(target, self.tables, self.schema)
            function_name = f'{migrate.identifier(self.schema)}."{contract.POSTGRES_REVISION_FUNCTION}"'
            target.execute(
                f"CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql "
                "SET search_path=pg_catalog AS $$ BEGIN RETURN NULL; END; $$"
            )
            with self.assertRaisesRegex(migrate.MigrationError, "unexpected_target_trigger"):
                migrate.verify_target_revision_triggers(target, self.schema)

    def test_postgres_same_named_trigger_with_changed_event_is_rejected(self):
        self.apply()
        contract = migrate._revision_contract()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            contract.install_opportunity_revision(target)
            _, definitions = contract.postgres_revision_definitions(self.schema)
            original = definitions["ff_radar_cache_v1_radar_jobs_insert"]
            target.execute(original.replace("AFTER INSERT", "BEFORE INSERT"))
            with self.assertRaisesRegex(migrate.MigrationError, "unexpected_target_trigger"):
                migrate.verify_target_revision_triggers(target, self.schema)

    def test_forward_self_reference_is_deferred_and_preserved(self):
        connection = sqlite3.connect(self.source)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            row = dict(connection.execute("SELECT * FROM space_runs").fetchone())
            original_id = row["id"]
            connection.execute("UPDATE space_runs SET cached_from_run_id='forward-run'")
            row.update(id="forward-run", cached_from_run_id=original_id)
            names = ",".join(migrate.identifier(name) for name in row)
            marks = ",".join("?" for _ in row)
            connection.execute(f"INSERT INTO space_runs({names}) VALUES({marks})", tuple(row.values()))
            connection.commit()
        finally:
            connection.close()
        self.snapshot = migrate.backup_sqlite(self.source, Path(self.directory.name) / "forward-backup")
        revised = migrate.read_sqlite(self.snapshot)
        self.addCleanup(revised.close)
        self.tables = migrate.inspect_schema(revised)
        self.digests = migrate.database_digest(revised, self.tables)
        self.assertEqual(self.apply(), "committed_and_verified")
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            self.assertEqual(migrate.database_digest(target, self.tables), self.digests)
            self.assertEqual(target.execute("SELECT COUNT(*) FROM space_runs").fetchone()[0], 2)

    def test_public_schema_privilege_is_not_accepted_as_identical_safe_target(self):
        self.apply()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            target.execute(f"GRANT USAGE ON SCHEMA {migrate.identifier(self.schema)} TO PUBLIC")
        with self.assertRaisesRegex(migrate.MigrationError, "not_private"):
            self.apply()

    def test_identity_next_value_exceeds_deleted_sqlite_high_water(self):
        self.apply()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            row = target.execute("INSERT INTO users (username,password_hash,created_at) VALUES (?,?,?) RETURNING id", ("sequence-test@example.invalid", FIXTURE_PASSWORD_HASH, "fixture-time")).fetchone()
            self.assertGreater(row["id"], self.tables[0].high_water)

    def test_case_insensitive_username_uniqueness_is_preserved(self):
        self.apply()
        with self.assertRaises(Exception):
            with migrate._connect_postgres(self.dsn, self.schema) as target:
                target.execute("INSERT INTO users (username,password_hash,created_at) VALUES (?,?,?)", ("migrationfixture@EXAMPLE.INVALID", FIXTURE_PASSWORD_HASH, "fixture-time"))

    def test_different_target_data_is_not_overwritten(self):
        self.apply()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            target.execute("UPDATE documents SET content=?", ("newer-target-document",))
        with self.assertRaisesRegex(migrate.MigrationError, "different_data"):
            self.apply()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            self.assertEqual(target.execute("SELECT content FROM documents").fetchone()[0], "newer-target-document")

    def test_hash_failure_rolls_back_all_schema_and_data(self):
        with mock.patch.object(migrate, "database_digest", return_value={}):
            with self.assertRaisesRegex(migrate.MigrationError, "hash_mismatch"):
                self.apply()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            self.assertEqual(migrate._target_objects(target, self.schema), [])

    def test_existing_partial_schema_is_not_filled_or_overwritten(self):
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            target.ensure_schema()
            target.execute("CREATE TABLE users (id BIGINT PRIMARY KEY)")
            target.execute("INSERT INTO users(id) VALUES(42)")
        with self.assertRaisesRegex(migrate.MigrationError, "not_empty"):
            self.apply()
        with migrate._connect_postgres(self.dsn, self.schema) as target:
            self.assertEqual(target.execute("SELECT id FROM users").fetchone()[0], 42)
            self.assertEqual(len(migrate._target_objects(target, self.schema)), 1)

    def test_active_migration_lock_prevents_duplicate_import(self):
        import psycopg

        with psycopg.connect(self.dsn) as owner:
            lock_key = int.from_bytes(hashlib.sha256(self.schema.encode()).digest()[:4], "big", signed=True)
            owner.execute("SELECT pg_advisory_xact_lock(%s, %s)", (1179798866, lock_key))
            with self.assertRaisesRegex(migrate.MigrationError, "already_running"):
                self.apply()


if __name__ == "__main__":
    unittest.main()
