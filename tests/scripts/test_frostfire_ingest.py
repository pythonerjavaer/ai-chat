import importlib.util
import io
import json
import os
import pathlib
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).parents[2] / "scripts" / "frostfire_ingest.py"
SPEC = importlib.util.spec_from_file_location("frostfire_ingest", SCRIPT_PATH)
assert SPEC and SPEC.loader
ingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingest)


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return self.body


class FrostfireIngestTests(unittest.TestCase):
    def valid_job(self, **overrides):
        job = {
            "company": "Example",
            "title": "2027 campus role",
            "city": "Shanghai",
            "official_url": "https://careers.example.com/jobs/1",
        }
        job.update(overrides)
        return job

    def test_normalizes_single_job_and_batch_shapes(self):
        job = self.valid_job()
        self.assertEqual(ingest.normalize_payload(job), {"jobs": [job]})
        self.assertEqual(ingest.normalize_payload([job]), {"jobs": [job]})
        self.assertEqual(ingest.normalize_payload({"jobs": [job]}), {"jobs": [job]})

    def test_normalizes_empty_heartbeat_with_batch_source(self):
        heartbeat = {
            "jobs": [],
            "source_id": "chatgpt-radar-01",
            "source_updated_at": "2026-08-23T10:00:00+10:00",
        }
        self.assertEqual(ingest.normalize_payload(heartbeat), heartbeat)
        self.assertEqual(
            ingest.normalize_payload({"source_id": "chatgpt-radar-02"}),
            {"jobs": [], "source_id": "chatgpt-radar-02"},
        )

    def test_rejects_empty_batch_without_source_and_more_than_ten_jobs(self):
        with self.assertRaisesRegex(ingest.InputError, "source_id is required"):
            ingest.normalize_payload({"jobs": []})
        with self.assertRaisesRegex(ingest.InputError, "at most 10"):
            ingest.normalize_payload([self.valid_job() for _ in range(11)])

    def test_rejects_extra_fields_at_batch_and_job_levels(self):
        with self.assertRaisesRegex(ingest.InputError, "batch contains unsupported"):
            ingest.normalize_payload({"jobs": [self.valid_job()], "cookie": "no"})
        with self.assertRaisesRegex(ingest.InputError, "job contains unsupported"):
            ingest.normalize_payload(self.valid_job(api_key="no"))

    def test_rejects_multiline_or_personal_contact_evidence(self):
        invalid_evidence = [
            "first line\nsecond line",
            "contact person@example.com",
            "contact 13800138000",
            "x" * 281,
        ]
        for evidence in invalid_evidence:
            with self.subTest(evidence=evidence[:30]):
                with self.assertRaises(ingest.InputError):
                    ingest.normalize_payload(self.valid_job(evidence=[evidence]))

    def test_accepts_maximum_evidence_and_rejects_invalid_source_timestamp(self):
        payload = ingest.normalize_payload(self.valid_job(
            source_updated_at="2026-08-23T10:00:00Z",
            evidence=["x" * 280],
        ))
        self.assertEqual(len(payload["jobs"][0]["evidence"][0]), 280)
        with self.assertRaisesRegex(ingest.InputError, "valid ISO 8601"):
            ingest.normalize_payload({
                "jobs": [],
                "source_id": "chatgpt-radar-01",
                "source_updated_at": "not-a-date",
            })

    def test_dry_run_does_not_read_secret_or_use_network(self):
        stdin = io.StringIO(json.dumps(self.valid_job()))
        stdout = io.StringIO()
        with (
            mock.patch.object(ingest.sys, "stdin", stdin),
            mock.patch.object(ingest.sys, "stdout", stdout),
            mock.patch.object(ingest, "load_token") as load_token,
            mock.patch.object(ingest.urllib.request, "urlopen") as urlopen,
        ):
            code = ingest.main(["--dry-run"])
        self.assertEqual(code, ingest.EXIT_OK)
        self.assertEqual(json.loads(stdout.getvalue())["jobs"], 1)
        load_token.assert_not_called()
        urlopen.assert_not_called()

    def test_dry_run_reports_empty_heartbeat_without_reading_secret(self):
        stdin = io.StringIO(json.dumps({
            "jobs": [],
            "source_id": "chatgpt-radar-01",
            "source_updated_at": "2026-08-23T10:00:00+10:00",
        }))
        stdout = io.StringIO()
        with (
            mock.patch.object(ingest.sys, "stdin", stdin),
            mock.patch.object(ingest.sys, "stdout", stdout),
            mock.patch.object(ingest, "load_token") as load_token,
        ):
            code = ingest.main(["--dry-run"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, ingest.EXIT_OK)
        self.assertTrue(result["heartbeat"])
        self.assertEqual(result["source_id"], "chatgpt-radar-01")
        load_token.assert_not_called()

    def test_environment_secret_is_preferred_to_keychain(self):
        with (
            mock.patch.dict(os.environ, {ingest.TOKEN_ENV: "environment-secret"}, clear=True),
            mock.patch.object(ingest, "read_keychain_token") as keychain,
        ):
            self.assertEqual(ingest.load_token(), "environment-secret")
        keychain.assert_not_called()

    def test_submit_uses_fixed_endpoint_and_secret_header(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(b'{"accepted":1,"skipped":[]}')

        with mock.patch.object(ingest.urllib.request, "urlopen", fake_urlopen):
            status, raw = ingest.submit_payload(
                {"jobs": [{"company": "Example"}]},
                "local-secret",
                12,
            )
        request = captured["request"]
        self.assertEqual(request.full_url, ingest.ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-recruitment-token"), "local-secret")
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"accepted": 1, "skipped": []})

    def test_missing_secret_has_stable_exit_code_and_never_echoes_input(self):
        stdin = io.StringIO(json.dumps(self.valid_job(title="secret-looking-title")))
        stderr = io.StringIO()
        with (
            mock.patch.object(ingest.sys, "stdin", stdin),
            mock.patch.object(ingest.sys, "stderr", stderr),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(ingest, "read_keychain_token", return_value=""),
        ):
            code = ingest.main([])
        self.assertEqual(code, ingest.EXIT_SECRET)
        self.assertNotIn("secret-looking-title", stderr.getvalue())

    def test_error_redaction_never_echoes_token(self):
        self.assertEqual(
            ingest.redact_secret("failure included local-secret", "local-secret"),
            "failure included [redacted]",
        )


if __name__ == "__main__":
    unittest.main()
