"""Account-owned application lists and durable notification acknowledgements."""

import json


def migrate(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS radar_saved_jobs (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL,
            job_json TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            saved_at TEXT NOT NULL,
            PRIMARY KEY(user_id, job_id)
        );
        CREATE TABLE IF NOT EXISTS radar_notification_cursors (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL DEFAULT 0
        );
    """)


def saved_jobs(connect, user_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM radar_saved_jobs WHERE user_id=? ORDER BY priority, saved_at, job_id",
            (user_id,),
        ).fetchall()
    return [{"job": json.loads(row["job_json"]), "priority": row["priority"]} for row in rows]


def save_job(connect, user_id, job, priority, now):
    with connect() as connection:
        connection.execute("""
            INSERT INTO radar_saved_jobs(user_id, job_id, job_json, priority, saved_at)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id, job_id) DO UPDATE SET
            job_json=excluded.job_json, priority=excluded.priority
        """, (user_id, job["id"], json.dumps(job, ensure_ascii=False), priority, now))


def pending_events(connect, user_id):
    with connect() as connection:
        latest = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM radar_events").fetchone()[0])
        first_running = connection.execute("""SELECT MIN(e.id) FROM radar_events e
            JOIN radar_runs r ON r.id=e.run_id WHERE r.status='running'""").fetchone()[0]
        initial = min(latest, int(first_running) - 1) if first_running is not None else latest
        # Begin with future updates, without presenting historical imports as new.
        connection.execute("""INSERT INTO radar_notification_cursors(user_id,event_id)
            VALUES(?,?) ON CONFLICT(user_id) DO NOTHING""", (user_id, initial))
        cursor = int(connection.execute(
            "SELECT event_id FROM radar_notification_cursors WHERE user_id=?", (user_id,),
        ).fetchone()[0])
        # Do not acknowledge past an unfinished run's events.
        running = connection.execute("""SELECT MIN(e.id) FROM radar_events e
            JOIN radar_runs r ON r.id=e.run_id WHERE r.status='running' AND e.id>?""", (cursor,)).fetchone()[0]
        ceiling = min(latest, int(running) - 1) if running is not None else latest
        rows = connection.execute("""SELECT id, entity_id FROM radar_events
            WHERE id>? AND id<=? AND event_type='NEW' AND entity_type='job'
            ORDER BY id LIMIT 100""", (cursor, ceiling)).fetchall()
    return [dict(row) for row in rows], (rows[-1]["id"] if len(rows) == 100 else ceiling)


def acknowledge(connect, user_id, event_id):
    with connect() as connection:
        latest = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM radar_events").fetchone()[0])
        if event_id > latest:
            raise ValueError("Invalid notification cursor")
        connection.execute("""UPDATE radar_notification_cursors SET event_id=?
            WHERE user_id=? AND event_id<?""", (event_id, user_id, event_id))
