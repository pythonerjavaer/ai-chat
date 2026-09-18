"""Explicit private application history does not manufacture public vacancies."""

import pytest

from backend.tests.test_radar_opportunities import harness  # noqa: F401
from backend.tests.test_postgres_application import persistent_app, register  # noqa: F401
from backend import database
from backend.future_radar import personal


BASE = "/api/future-radar/application-records"


def test_records_are_durable_and_idempotent_without_inventing_jobs(harness, monkeypatch):
    h = harness
    job = h.insert("real-role", company="示例企业")
    monkeypatch.setattr(database, "utc_now", lambda: "2026-09-19T01:00:00Z")
    first = h.client.put(BASE + "/confirmed-001", headers=h.auth, json={
        "company": "  示例企业  ", "title": " ", "batch": " 2027 秋招 ",
    })
    assert first.status_code == 200, first.text
    record = first.json()
    assert record["company"] == "示例企业"
    assert record["title"] is None
    assert record["confirmed_date"] is None
    assert record["batch"] == "2027 秋招"
    assert record["status"] == "applied"
    assert "user_id" not in record and "job_id" not in record
    monkeypatch.setattr(database, "utc_now", lambda: "2026-09-19T02:00:00Z")
    second = h.client.put(BASE + "/confirmed-001", headers=h.auth, json={
        "company": "示例企业", "title": "实际报名的产品经理", "batch": "2027 秋招",
        "location": "上海", "confirmed_date": "2026-09-18", "notes": "用户确认已报名",
    })
    assert second.status_code == 200, second.text
    assert second.json()["created_at"] == record["created_at"]
    assert second.json()["updated_at"] != record["updated_at"]
    with database.connect() as connection:
        personal.migrate(connection)  # A later startup preserves the private ledger.
    result = h.client.get(BASE, headers=h.auth).json()
    assert result == {"items": [second.json()], "total": 1, "page": 1,
                      "page_size": 100, "has_more": False}
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_jobs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM radar_applications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM radar_events").fetchone()[0] == 0
    detail = h.client.get("/api/future-radar/opportunities/" + job["id"], headers=h.auth).json()
    assert detail["application_status"] == "not_applied"


def test_records_display_known_brand_aliases_once_but_preserve_actual_subsidiaries(harness):
    h = harness
    canonical = h.client.put(BASE + "/blackrock", headers=h.auth, json={"company": "贝莱德"})
    assert canonical.status_code == 200
    assert canonical.json()["company"] == "BlackRock"
    subsidiary = h.client.put(BASE + "/ping-an-bank", headers=h.auth, json={"company": "平安银行"})
    assert subsidiary.status_code == 200
    assert subsidiary.json()["company"] == "平安银行"
    names = {record["company"] for record in h.client.get(BASE, headers=h.auth).json()["items"]}
    assert names == {"BlackRock", "平安银行"}


def test_private_records_never_cross_accounts_and_delete_is_idempotent(harness):
    h = harness
    other_auth, _ = register(h.client, "history-other-user")
    endpoint = BASE + "/same-anonymous-key"
    h.client.put(endpoint, headers=h.auth, json={"company": "First company"}).raise_for_status()
    assert h.client.get(BASE, headers=other_auth).json()["items"] == []
    h.client.put(endpoint, headers=other_auth, json={"company": "Second company"}).raise_for_status()
    assert h.client.get(BASE, headers=h.auth).json()["items"][0]["company"] == "First company"
    assert h.client.get(BASE, headers=other_auth).json()["items"][0]["company"] == "Second company"
    for _ in range(2):
        assert h.client.delete(endpoint, headers=other_auth).json() == {"deleted": True}
    assert h.client.get(BASE, headers=other_auth).json()["total"] == 0
    assert h.client.get(BASE, headers=h.auth).json()["total"] == 1
    for method, url, kwargs in [("get", BASE, {}), ("put", endpoint, {"json": {"company": "Unauthenticated"}}),
                                ("delete", endpoint, {})]:
        assert getattr(h.client, method)(url, **kwargs).status_code in (401, 403)


def test_record_pagination_is_bounded_stable_and_keeps_unspecified_roles(harness):
    h = harness
    for index in range(3):
        h.client.put(BASE + f"/key-{index}", headers=h.auth, json={
            "company": f"公司 {index}", "title": None,
        }).raise_for_status()
    first = h.client.get(BASE, headers=h.auth, params={"page_size": 2}).json()
    second = h.client.get(BASE, headers=h.auth, params={"page_size": 2, "page": 2}).json()
    assert first["total"] == second["total"] == 3
    assert first["has_more"] is True and second["has_more"] is False
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert len({item["record_key"] for item in first["items"] + second["items"]}) == 3
    assert all(item["title"] is None for item in first["items"] + second["items"])
    for params in ({"page_size": 201}, {"page_size": 0}, {"page": 0}):
        assert h.client.get(BASE, headers=h.auth, params=params).status_code == 422


@pytest.mark.parametrize("payload", [
    {"company": " "}, {"company": "x" * 161}, {"company": "真实企业", "title": "x" * 241},
    {"company": "真实企业", "status": "planned"},
    {"company": "真实企业", "confirmed_date": "2026-99-99"},
    {"company": "真实企业", "chat_url": "private source must never be retained"},
    {"company": "真实企业", "user_id": 100},
    {"company": "真实企业", "notes": "https://chatgpt.com/c/private-conversation"},
])
def test_records_reject_invalid_or_unexpected_private_source_fields(harness, payload):
    assert harness.client.put(BASE + "/safe-key", headers=harness.auth, json=payload).status_code == 422
    assert harness.client.get(BASE, headers=harness.auth).json()["total"] == 0


@pytest.mark.parametrize("record_key", ["key with spaces", "x" * 121, "私人对话标识"])
def test_record_keys_are_small_and_url_safe(harness, record_key):
    assert harness.client.put(BASE + "/" + record_key, headers=harness.auth,
                              json={"company": "真实企业"}).status_code == 422
    assert harness.client.delete(BASE + "/" + record_key, headers=harness.auth).status_code == 422


def test_records_are_removed_when_owner_is_deleted(harness):
    h = harness
    h.client.put(BASE + "/deleted-owner-record", headers=h.auth, json={"company": "真实企业"}).raise_for_status()
    with database.connect() as connection:
        user_id = connection.execute("SELECT user_id FROM radar_application_records").fetchone()[0]
        connection.execute("DELETE FROM users WHERE id=?", (user_id,))
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM radar_application_records").fetchone()[0] == 0


def test_postgres_application_records_survive_pool_restart_and_remain_private(persistent_app):
    from fastapi.testclient import TestClient
    from backend.storage import close_postgres_pools

    app = persistent_app
    with TestClient(app.main.app) as client:
        auth, user = register(client, "private-history-user")
        other_auth, _ = register(client, "private-history-other")
        payload = {"company": "历史报名企业", "batch": "本轮秋招", "confirmed_date": "2026-09-19"}
        for _ in range(2):
            response = client.put(BASE + "/confirmed-pg-record", headers=auth, json=payload)
            assert response.status_code == 200, response.text
        close_postgres_pools()
        assert client.get(BASE, headers=other_auth).json()["total"] == 0
        result = client.get(BASE, headers=auth).json()
        assert result["total"] == 1 and result["items"][0]["title"] is None
        assert result["items"][0]["confirmed_date"] == "2026-09-19"
        with app.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM radar_applications").fetchone()[0] == 0
            connection.execute("DELETE FROM users WHERE id=?", (user["id"],))
        with app.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM radar_application_records").fetchone()[0] == 0
