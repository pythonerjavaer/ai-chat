import io
import urllib.error
import urllib.parse
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.tests.test_future_radar import radar_service, create_source, sample_job  # noqa: F401
from backend import main
from backend.future_radar.webhook import signing_key
from scripts import frostfire_source_import as sender


def test_actual_monitor_sender_signed_delivery_replay_and_history_api(radar_service, monkeypatch):
    token = "test-only-existing-monitor-token-32-bytes-minimum"
    options = {**vars(main.settings), "future_radar_webhook_secret": "", "recruitment_ingest_token": token,
               "future_radar_enabled": False, "recruitment_refresh_minutes": 0}
    monkeypatch.setattr(main, "settings", SimpleNamespace(**options))
    monkeypatch.setattr(main, "future_radar_service", radar_service)
    create_source(radar_service, "monitor-integration")
    payload = {"version": "FROSTFIRE_SYNC_V1", "source_id": "monitor-integration", "batch_id": "delivery-1",
               "jobs": [sample_job("history-integration", verification_status="verified")]}
    seen = []
    with TestClient(main.app) as client:
        def open_request(request, timeout):
            assert timeout > 0
            seen.append(request)
            response = client.post(urllib.parse.urlsplit(request.full_url).path, content=request.data, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise urllib.error.HTTPError(request.full_url, response.status_code, "test response", None, io.BytesIO())
            return io.BytesIO(response.content)
        monkeypatch.setattr(sender.urllib.request, "urlopen", open_request)
        first = sender.submit_payload(payload, token, 15)
        assert first["delivery"] == "processed"
        assert first["counts"]["new_jobs"] == 1
        replay = sender.submit_payload(payload, token, 15)
        assert replay["idempotent_replay"] is True
        assert all(request.full_url.endswith("/webhook") for request in seen)
        assert not any(request.has_header("X-recruitment-token") for request in seen)
        job = radar_service.repository.get_job("history-integration")
        assert job is not None
        path = f"/api/future-radar/jobs/{job['id']}/history"
        assert client.get(path).status_code == 401
        registered = client.post("/api/auth/register", json={"username": "history-viewer", "password": "test-history-password-123", "privacy_accepted": True})
        assert registered.status_code == 201
        auth = {"Authorization": "Bearer " + registered.json()["access_token"]}
        response = client.get(path, headers=auth)
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["job"]["closing_date"] == "2027-09-15"
        delivery_status = client.get("/api/future-radar/integrations/status", headers=auth).json()["webhook"]
        assert delivery_status["enabled"] is True
        assert delivery_status["events"] == 1 and delivery_status["deliveries"] == 2
        assert client.get(path + "?valid_at=2026-01-01T00:00:00", headers=auth).status_code == 422
        assert client.post(path + "/corrections", headers=auth, json={}).status_code in {401, 422}


def test_signature_keys_fail_closed_and_do_not_accept_root_token_as_child():
    token = "test-only-strong-existing-monitor-token-for-signing"
    derived = signing_key(ingest_token=token)
    assert derived and derived != token
    assert signing_key(explicit="short", ingest_token=token) == ""
    assert signing_key(ingest_token="short") == ""


def test_sender_auto_fallback_reuses_identical_payload_and_idempotency_key(monkeypatch):
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "temporarily unavailable", None, io.BytesIO())
        return io.BytesIO(b'{"idempotent_replay":true}')
    monkeypatch.setattr(sender.urllib.request, "urlopen", open_request)
    result = sender.submit_payload({"version": "FROSTFIRE_SYNC_V1", "source_id": "monitor-integration", "batch_id": "stable-event", "jobs": []},
                                   "test-only-strong-existing-monitor-token-for-signing", 15)
    assert result["idempotent_replay"] is True
    assert requests[0].full_url.endswith("/webhook")
    assert requests[1].full_url.endswith("/sync")
    assert requests[0].data == requests[1].data
    assert requests[0].get_header("Idempotency-key") == requests[1].get_header("Idempotency-key")
