"""Upload intents: reserve, upload to the signed URL, complete, then download."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from server.app import create_app
from server.config import Settings

PASSWORD = "dashboard-password-1234"
ADMIN_KEY = "test-admin-key-0123456789"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        admin_key=ADMIN_KEY,
        dashboard_password=PASSWORD,
    )


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def admin_headers():
    return {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def drive(client, admin_headers) -> dict:
    response = client.post("/api/drives", json={"name": "Stick"}, headers=admin_headers)
    assert response.status_code == 201
    return response.json()


def agent_headers(drive) -> dict[str, str]:
    return {"Authorization": f"Bearer {drive['token']}"}


def start_upload(client, headers, drive_id, name, size, note=None):
    payload = {"filename": name, "size": size}
    if note is not None:
        payload["note"] = note
    return client.post(
        f"/api/drives/{drive_id}/files/upload-url", headers=headers, json=payload
    )


def test_presigned_upload_end_to_end(client, admin_headers, drive):
    content = b"conference slides" * 100
    sha256 = hashlib.sha256(content).hexdigest()

    intent = start_upload(
        client, admin_headers, drive["id"], "slides.txt", len(content), note="for you"
    )
    assert intent.status_code == 201, intent.text
    reservation = intent.json()
    assert reservation["upload_url"].startswith("/api/blob/upload/")

    uploaded = client.put(reservation["upload_url"], content=content)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["size"] == len(content)

    # Not in the queue until completion.
    assert client.get(
        "/api/agent/queue", headers=agent_headers(drive)
    ).json()["files"] == []

    completed = client.post(
        f"/api/drives/{drive['id']}/files/{reservation['id']}/complete",
        headers=admin_headers,
        json={"sha256": sha256},
    )
    assert completed.status_code == 200, completed.text
    record = completed.json()
    assert record["sha256"] == sha256
    assert record["note"] == "for you"

    queue = client.get("/api/agent/queue", headers=agent_headers(drive)).json()["files"]
    assert [f["filename"] for f in queue] == ["slides.txt"]

    link = client.get(
        f"/api/agent/files/{reservation['id']}/link", headers=agent_headers(drive)
    )
    assert link.status_code == 200
    link_body = link.json()
    assert link_body["sha256"] == sha256
    download = client.get(link_body["url"])
    assert download.status_code == 200
    assert download.content == content

    ack = client.post(
        f"/api/agent/files/{reservation['id']}/ack",
        headers=agent_headers(drive),
        data={"sha256": sha256},
    )
    assert ack.status_code == 200


def test_complete_rejects_a_size_mismatch_and_removes_the_row(client, admin_headers, drive, settings):
    intent = start_upload(client, admin_headers, drive["id"], "small.txt", 10).json()
    assert client.put(intent["upload_url"], content=b"tiny").status_code == 200

    response = client.post(
        f"/api/drives/{drive['id']}/files/{intent['id']}/complete",
        headers=admin_headers,
        json={"sha256": hashlib.sha256(b"tiny").hexdigest()},
    )
    assert response.status_code == 422
    assert "expected" in response.json()["detail"]
    assert client.get(
        f"/api/drives/{drive['id']}/files", headers=admin_headers
    ).json() == []
    assert list(settings.files_dir.iterdir()) == []


def test_intent_rejects_blocked_types_and_oversized_files(client, admin_headers, drive):
    blocked = start_upload(client, admin_headers, drive["id"], "setup.exe", 10)
    assert blocked.status_code == 415

    too_big = start_upload(client, admin_headers, drive["id"], "big.txt", 51 * 1024 * 1024)
    assert too_big.status_code == 413


def test_intents_and_completion_require_owner_auth(client, drive):
    assert start_upload(client, {}, drive["id"], "a.txt", 1).status_code == 401
    assert client.post(
        f"/api/drives/{drive['id']}/files/x/complete", json={"sha256": "0" * 64}
    ).status_code == 401


def test_dashboard_session_can_upload(client, drive, admin_headers):
    from server.app import SESSION_COOKIE

    assert client.post(
        "/dashboard/login", data={"password": PASSWORD}, follow_redirects=False
    ).status_code == 303
    assert SESSION_COOKIE in client.cookies
    session_headers = {"X-Dashboard-Request": "1"}

    content = b"session bytes"
    intent = start_upload(
        client, session_headers, drive["id"], "note.txt", len(content)
    )
    assert intent.status_code == 201, intent.text
    reservation = intent.json()
    assert client.put(reservation["upload_url"], content=content).status_code == 200
    done = client.post(
        f"/api/drives/{drive['id']}/files/{reservation['id']}/complete",
        headers=session_headers,
        json={"sha256": hashlib.sha256(content).hexdigest()},
    )
    assert done.status_code == 200, done.text
    assert client.get("/api/dashboard/drives").json()["drives"][0]["pending_count"] == 1

    # The completion call is still refused without the CSRF header.
    reservation2 = start_upload(
        client, session_headers, drive["id"], "two.txt", 1
    ).json()
    client.put(reservation2["upload_url"], content=b"x")
    assert client.post(
        f"/api/drives/{drive['id']}/files/{reservation2['id']}/complete",
        json={"sha256": hashlib.sha256(b"x").hexdigest()},
    ).status_code == 403


def test_blob_endpoints_reject_bad_signatures(client):
    assert client.put("/api/blob/upload/" + "a" * 32 + "?expires=9&signature=nope",
                      content=b"x").status_code == 403
    assert client.get("/api/blob/download/" + "a" * 32 + "?expires=9&signature=nope").status_code == 403
    # Unknown keys never reach the filesystem.
    assert client.get("/api/blob/download/../etc/passwd?expires=9&signature=x").status_code in (403, 404)
