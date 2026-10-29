"""Postgres backend tests. Skipped unless DC_TEST_DATABASE_URL is set.

Run them with a throwaway database:

    docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
    DC_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres \
        .venv/bin/python -m pytest tests/test_postgres.py -q
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path

import pytest

from server.app import create_app
from server.config import Settings
from server.db import Database

DSN = os.environ.get("DC_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="DC_TEST_DATABASE_URL is not set")


@pytest.fixture
def db() -> Database:
    database = Database(url=DSN)
    with database.connect() as conn:
        conn.execute("TRUNCATE drives, files CASCADE")
    return database


def test_drive_and_file_lifecycle(db: Database):
    drive = db.create_drive(uuid.uuid4().hex, "Sam's stick", "hash-" + uuid.uuid4().hex)
    assert drive["name"] == "Sam's stick"
    assert db.get_drive(drive["id"])["installed_at"] is None
    assert db.get_drive_by_token_hash(drive["token_hash"])["id"] == drive["id"]

    db.touch_drive(drive["id"])
    db.mark_installed(drive["id"])
    listed = db.list_drives()
    assert listed[0]["pending_count"] == 0
    assert listed[0]["last_seen_at"] is not None
    assert listed[0]["installed_at"] is not None

    record = db.create_upload_intent(
        uuid.uuid4().hex, drive["id"], "notes.txt", 5, "hi", uuid.uuid4().hex
    )
    assert db.list_files(drive["id"]) == []
    assert db.pending_files(drive["id"]) == []

    db.mark_uploaded(record["id"], hashlib.sha256(b"hello").hexdigest())
    files = db.list_files(drive["id"])
    assert [f["filename"] for f in files] == ["notes.txt"]
    assert db.pending_files(drive["id"])[0]["id"] == record["id"]
    assert db.list_drives()[0]["pending_count"] == 1

    time.sleep(0.01)
    db.mark_delivered(record["id"])
    assert db.pending_files(drive["id"]) == []
    assert db.list_drives()[0]["pending_count"] == 0
    assert db.list_files(drive["id"])[0]["delivered_at"] is not None

    old_token_hash = drive["token_hash"]
    db.mark_uploaded(record["id"], "f" * 64)  # second call is harmless
    db.set_token_hash(drive["id"], "rotated-" + uuid.uuid4().hex)
    assert db.get_drive_by_token_hash(old_token_hash) is None
    db.revoke_drive(drive["id"])
    assert db.get_drive(drive["id"])["revoked"] == 1
    db.delete_file(record["id"])
    assert db.list_files(drive["id"]) == []


def test_app_runs_against_postgres(db, tmp_path: Path):
    from fastapi.testclient import TestClient

    settings = Settings(
        data_dir=tmp_path / "data",
        admin_key="postgres-admin-key-123456",
        dashboard_password="postgres-dashboard-1234",
        database_url=DSN,
    )
    headers = {"X-Admin-Key": settings.admin_key}
    content = b"postgres upload"

    with TestClient(create_app(settings)) as client:
        drive = client.post("/api/drives", json={"name": "PG"}, headers=headers).json()
        intent = client.post(
            f"/api/drives/{drive['id']}/files/upload-url",
            headers=headers,
            json={"filename": "pg.txt", "size": len(content), "note": "db"},
        ).json()
        assert client.put(intent["upload_url"], content=content).status_code == 200
        done = client.post(
            f"/api/drives/{drive['id']}/files/{intent['id']}/complete",
            headers=headers,
            json={"sha256": hashlib.sha256(content).hexdigest()},
        )
        assert done.status_code == 200, done.text

        queue = client.get(
            "/api/agent/queue",
            headers={"Authorization": f"Bearer {drive['token']}"},
        ).json()
        assert [f["filename"] for f in queue["files"]] == ["pg.txt"]
        dashboard = client.get("/api/dashboard/drives", headers=headers).json()
        assert dashboard["drives"][0]["pending_count"] == 1
