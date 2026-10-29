"""API behaviour: auth, queueing, download, checksums, limits and revocation."""

from __future__ import annotations

import hashlib

import pytest


def upload(client, headers, drive_id, name="hello.txt", content=b"hello world", note=None):
    data = {"note": note} if note is not None else {}
    return client.post(
        f"/api/drives/{drive_id}/files",
        headers=headers,
        files={"file": (name, content, "text/plain")},
        data=data,
    )


def agent_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- owner auth ---------------------------------------------------------


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Admin-Key": "wrong"}, {"X-Admin-Key": "test-admin-key"}],
)
def test_owner_endpoints_require_admin_key(client, headers):
    assert client.get("/api/drives", headers=headers).status_code == 401
    assert client.post("/api/drives", json={"name": "x"}, headers=headers).status_code == 401


def test_create_drive_returns_token_once_and_lists_it(client, admin_headers, new_drive):
    assert new_drive["name"] == "Test stick"
    assert new_drive["token"].startswith("dc_")
    assert new_drive["revoked"] is False
    assert new_drive["pending_count"] == 0

    listed = client.get("/api/drives", headers=admin_headers).json()
    assert [d["id"] for d in listed] == [new_drive["id"]]
    assert "token" not in listed[0]


def test_empty_drive_name_rejected(client, admin_headers):
    assert client.post("/api/drives", json={"name": "  "}, headers=admin_headers).status_code == 422
    assert client.post("/api/drives", json={"name": ""}, headers=admin_headers).status_code == 422


# -- uploads and the agent queue ---------------------------------------


def test_upload_queues_file_for_agent(client, admin_headers, new_drive):
    body = b"payload bytes"
    response = upload(client, admin_headers, new_drive["id"], "notes.txt", body, note="hi")
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["sha256"] == hashlib.sha256(body).hexdigest()
    assert record["size"] == len(body)
    assert record["note"] == "hi"
    assert record["delivered_at"] is None

    queue = client.get("/api/agent/queue", headers=agent_headers(new_drive["token"])).json()
    assert [f["filename"] for f in queue["files"]] == ["notes.txt"]

    drives = client.get("/api/drives", headers=admin_headers).json()
    assert drives[0]["pending_count"] == 1


def test_agent_cannot_use_admin_key_or_bad_token(client, new_drive, admin_headers):
    assert client.get("/api/agent/queue").status_code == 401
    assert client.get("/api/agent/queue", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get(
        "/api/agent/queue", headers=agent_headers("dc_nope")
    ).status_code == 401


def test_download_then_ack_flow(client, admin_headers, new_drive):
    content = b"journal page\n" * 100
    record = upload(client, admin_headers, new_drive["id"], "page.txt", content).json()
    auth = agent_headers(new_drive["token"])

    download = client.get(f"/api/agent/files/{record['id']}", headers=auth)
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["x-content-sha256"] == record["sha256"]

    bad = client.post(
        f"/api/agent/files/{record['id']}/ack", headers=auth, data={"sha256": "0" * 64}
    )
    assert bad.status_code == 422
    assert client.get("/api/agent/queue", headers=auth).json()["files"]

    ok = client.post(
        f"/api/agent/files/{record['id']}/ack",
        headers=auth,
        data={"sha256": record["sha256"]},
    )
    assert ok.status_code == 200
    assert client.get("/api/agent/queue", headers=auth).json()["files"] == []
    listed = client.get(f"/api/drives/{new_drive['id']}/files", headers=admin_headers).json()
    assert listed[0]["delivered_at"] is not None


def test_drives_are_isolated(client, admin_headers, new_drive):
    other = client.post("/api/drives", json={"name": "Other"}, headers=admin_headers).json()
    record = upload(client, admin_headers, new_drive["id"], "private.txt", b"secret").json()
    auth_other = agent_headers(other["token"])

    assert client.get("/api/agent/queue", headers=auth_other).json()["files"] == []
    assert client.get(f"/api/agent/files/{record['id']}", headers=auth_other).status_code == 404
    assert client.delete(
        f"/api/drives/{other['id']}/files/{record['id']}", headers=admin_headers
    ).status_code == 404


# -- safety rules -------------------------------------------------------


@pytest.mark.parametrize("name", ["setup.exe", "RUN.BAT", "script.sh", "app.msi"])
def test_executable_types_are_blocked(client, admin_headers, new_drive, name):
    response = upload(client, admin_headers, new_drive["id"], name, b"MZ...")
    assert response.status_code == 415
    assert client.get(
        f"/api/drives/{new_drive['id']}/files", headers=admin_headers
    ).json() == []


def test_filename_is_sanitized(client, admin_headers, new_drive):
    record = upload(
        client, admin_headers, new_drive["id"], "../../etc/passwd", b"data"
    ).json()
    assert record["filename"] == "passwd"


def test_size_limit_rejects_and_leaves_no_file(tmp_path, admin_headers, new_drive):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.config import Settings

    settings = Settings(data_dir=tmp_path / "small", admin_key=admin_headers["X-Admin-Key"],
                        max_file_bytes=16)
    app = create_app(settings)
    with TestClient(app) as client:
        drive = client.post("/api/drives", json={"name": "tiny"}, headers=admin_headers).json()
        response = upload(client, admin_headers, drive["id"], "big.txt", b"x" * 17)
        assert response.status_code == 413
        assert list(settings.files_dir.iterdir()) == []


def test_empty_file_rejected(client, admin_headers, new_drive):
    assert upload(client, admin_headers, new_drive["id"], "empty.txt", b"").status_code == 415


def test_note_too_long_rejected(client, admin_headers, new_drive):
    response = upload(client, admin_headers, new_drive["id"], "n.txt", b"x", note="y" * 2001)
    assert response.status_code == 422


# -- revocation, rotation, deletion -------------------------------------


def test_revoke_stops_the_agent(client, admin_headers, new_drive):
    upload(client, admin_headers, new_drive["id"], "a.txt", b"a")
    auth = agent_headers(new_drive["token"])
    assert client.get("/api/agent/queue", headers=auth).status_code == 200

    assert client.post(
        f"/api/drives/{new_drive['id']}/revoke", headers=admin_headers
    ).status_code == 200
    assert client.get("/api/agent/queue", headers=auth).status_code == 403
    assert client.get("/api/agent/me", headers=auth).status_code == 403


def test_rotate_token_invalidates_old_one(client, admin_headers, new_drive):
    rotated = client.post(
        f"/api/drives/{new_drive['id']}/rotate-token", headers=admin_headers
    ).json()
    assert rotated["token"] != new_drive["token"]
    assert client.get(
        "/api/agent/queue", headers=agent_headers(new_drive["token"])
    ).status_code == 401
    assert client.get(
        "/api/agent/queue", headers=agent_headers(rotated["token"])
    ).status_code == 200


def test_delete_file_removes_record_and_bytes(client, admin_headers, new_drive, app):
    record = upload(client, admin_headers, new_drive["id"], "temp.txt", b"temp").json()
    stored = app.state.db.get_file(record["id"])
    path = app.state.storage.path_for(stored["storage_key"])
    assert path.is_file()

    response = client.delete(
        f"/api/drives/{new_drive['id']}/files/{record['id']}", headers=admin_headers
    )
    assert response.status_code == 200
    assert not path.exists()
    assert client.get(
        f"/api/drives/{new_drive['id']}/files", headers=admin_headers
    ).json() == []
