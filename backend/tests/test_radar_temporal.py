import pytest

from backend.future_radar.temporal import correct, history, instant, observe
from backend.tests.test_postgres_radar_lock import lock_database  # noqa: F401


@pytest.fixture
def job_database(lock_database):
    with lock_database.connect() as connection:
        connection.execute("""INSERT INTO radar_jobs
            (id,external_id,company,title,verification_status,content_hash,
             first_seen_at,last_seen_at,last_changed_at,created_at,updated_at,closing_date)
            VALUES ('history-job','history-external','企业','数据分析岗','verified','hash',?,?,?,?,?,'2026-01-31')""",
            ["2026-01-01T00:00:00+00:00"] * 5)
        row = dict(connection.execute("SELECT * FROM radar_jobs WHERE id='history-job'").fetchone())
        observe(connection, [row], recorded_at="2026-01-01T00:00:00+00:00")
    return lock_database


def test_observed_change_and_retroactive_correction_preserve_both_time_axes(job_database):
    with job_database.connect() as connection:
        job = dict(connection.execute("SELECT * FROM radar_jobs WHERE id='history-job'").fetchone())
        job["closing_date"] = "2026-02-15"
        observe(connection, [job], recorded_at="2026-01-10T00:00:00+00:00")
        old = history(connection, job["id"], valid_at="2026-01-05T00:00:00Z", known_at="2026-01-07T00:00:00Z")
        assert old["items"][0]["job"]["closing_date"] == "2026-01-31"
        revision = history(connection, job["id"])["revision"]
        corrected = correct(connection, job["id"], valid_from="2026-01-03T00:00:00Z", valid_to="2026-01-08T00:00:00Z",
                            changes={"closing_date": "2026-02-20"}, expected_revision=revision,
                            reason="官网公告确认该期间截止日期应为2月20日")
        assert corrected["corrected"] is True
        current = history(connection, job["id"], valid_at="2026-01-05T00:00:00Z")
        assert current["items"][0]["job"]["closing_date"] == "2026-02-20"
        old_again = history(connection, job["id"], valid_at="2026-01-05T00:00:00Z", known_at="2026-01-07T00:00:00Z")
        assert old_again["items"][0]["job"]["closing_date"] == "2026-01-31"
        outside = history(connection, job["id"], valid_at="2026-01-09T00:00:00Z")
        assert outside["items"][0]["job"]["closing_date"] == "2026-01-31"
        future = history(connection, job["id"], valid_at="2026-01-10T00:00:00Z")
        assert future["items"][0]["job"]["closing_date"] == "2026-02-15"
        assert not history(connection, job["id"], known_at="2025-12-31T23:59:59Z")["items"]


def test_unchanged_observation_does_not_grow_history_and_changes_roll_back(job_database):
    connection = job_database.connect()
    job = dict(connection.execute("SELECT * FROM radar_jobs WHERE id='history-job'").fetchone())
    observe(connection, [job], recorded_at="2026-01-02T00:00:00Z")
    assert connection.execute("SELECT COUNT(*) FROM radar_job_versions").fetchone()[0] == 1
    connection.commit()
    job["status"] = "closed"
    observe(connection, [job], recorded_at="2026-01-03T00:00:00Z")
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM radar_job_versions").fetchone()[0] == 1
    connection.close()


def test_correction_rejects_stale_revisions_and_unknown_history(job_database):
    with job_database.connect() as connection:
        revision = history(connection, "history-job")["revision"]
        common = dict(changes={"status": "closed"}, reason="已确认的官网历史关闭信息", expected_revision=revision)
        with pytest.raises(ValueError, match="predates"):
            correct(connection, "history-job", valid_from="2025-12-01T00:00:00Z", valid_to="2025-12-03T00:00:00Z", **common)
        with pytest.raises(ValueError, match="reload"):
            correct(connection, "history-job", valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-03T00:00:00Z",
                    **{**common, "expected_revision": "old"})
        with pytest.raises(ValueError, match="timezone"):
            instant("2026-01-01T00:00:00")


def test_public_history_hides_unverified_facts(job_database):
    with job_database.connect() as connection:
        job = dict(connection.execute("SELECT * FROM radar_jobs WHERE id='history-job'").fetchone())
        job.update(verification_status="pending", title="unverified claim")
        observe(connection, [job], recorded_at="2026-01-02T00:00:00Z")
        result = history(connection, job["id"], valid_at="2026-01-03T00:00:00Z")
        assert result["items"] == []
