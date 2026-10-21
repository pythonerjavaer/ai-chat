import os
import sqlite3
from unittest.mock import patch

with patch.dict(os.environ, {"JWT_SECRET": "startup-cache-tests-only"}):
    from backend import database


def test_startup_schema_cache_tracks_release_ids(tmp_path):
    database_path = tmp_path / "startup-cache.sqlite3"

    def connect():
        return sqlite3.connect(database_path)

    assert not database.startup_schema_version_is_current(
        "release-a", connection_factory=connect
    )

    database.mark_startup_schema_version_current("release-a", connection_factory=connect)

    assert database.startup_schema_version_is_current(
        "release-a", connection_factory=connect
    )
    assert not database.startup_schema_version_is_current(
        "release-b", connection_factory=connect
    )


def test_empty_release_id_never_enables_startup_schema_cache():
    assert not database.startup_schema_version_is_current("")
    database.mark_startup_schema_version_current("")
