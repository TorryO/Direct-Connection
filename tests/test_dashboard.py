"""Owner dashboard: login, session cookie and the drive overview."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from server.app import SESSION_COOKIE, create_app
from server.config import Settings

PASSWORD = "dashboard-password-1234"
ADMIN_KEY = "test-admin-key-0123456789"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", admin_key=ADMIN_KEY, dashboard_password=PASSWORD
    )


@pytest.fixture
def client(settings: Settings):
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": ADMIN_KEY}


def create_drive(client, admin_headers, name="Sam's stick") -> dict:
    response = client.post("/api/drives", json={"name": name}, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


def login(client):
    response = client.post(
        "/dashboard/login", data={"password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    assert SESSION_COOKIE in client.cookies
    return response


# -- login ---------------------------------------------------------------


def test_dashboard_is_disabled_without_a_password(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(Settings(data_dir=tmp_path / "d", admin_key=ADMIN_KEY))
    with TestClient(app) as anonymous:
        page = anonymous.get("/dashboard")
        assert page.status_code == 503
        assert "DC_DASHBOARD_PASSWORD" in page.text
        assert anonymous.post("/dashboard/login", data={"password": "x"}).status_code == 503


def test_login_page_is_shown_to_anonymous_visitors(client):
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert 'type="password"' in page.text
    assert "Sign in" in page.text


def test_wrong_password_is_rejected(client):
    response = client.post("/dashboard/login", data={"password": "wrong"})
    assert response.status_code == 401
    assert "Wrong password" in response.text
    assert SESSION_COOKIE not in client.cookies


def test_login_sets_a_session_and_shows_the_overview(client, admin_headers):
    create_drive(client, admin_headers)
    login(client)

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Program installed" in page.text
    assert 'id="drives"' in page.text
    assert '<dialog id="modal">' in page.text
    for action in ('"Send"', '"Revoke"', '"Rotate"'):
        assert action in page.text
    assert page.headers["cache-control"] == "no-store"

    data = client.get("/api/dashboard/drives").json()
    assert [d["name"] for d in data["drives"]] == ["Sam's stick"]


def test_logout_clears_the_session(client):
    login(client)
    response = client.post("/dashboard/logout", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/dashboard/drives").status_code == 401
    assert 'type="password"' in client.get("/dashboard").text


def test_forged_or_expired_sessions_are_rejected(client, admin_headers):
    create_drive(client, admin_headers)
    client.cookies.set(SESSION_COOKIE, "9999999999.deadbeef")
    assert client.get("/api/dashboard/drives").status_code == 401
    assert 'type="password"' in client.get("/dashboard").text

    # A correctly signed value from before the TTL is still refused.
    from server.app import _sign_session

    old = int(time.time()) - 60 * 60 * 24
    client.cookies.set(SESSION_COOKIE, f"{old}.{_sign_session(ADMIN_KEY, old)}")
    assert client.get("/api/dashboard/drives").status_code == 401


def test_overview_api_accepts_the_admin_key(client, admin_headers):
    create_drive(client, admin_headers, "Key stick")
    assert client.get("/api/dashboard/drives").status_code == 401
    response = client.get("/api/dashboard/drives", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["drives"][0]["name"] == "Key stick"


# -- installed / online state -------------------------------------------


def test_new_drive_is_neither_installed_nor_online(client, admin_headers):
    create_drive(client, admin_headers)
    drive = client.get("/api/dashboard/drives", headers=admin_headers).json()["drives"][0]
    assert drive["installed"] is False
    assert drive["online"] is False
    assert drive["agent_seen"] is False
    assert drive["installed_at"] is None


def test_provisioning_marks_it_installed_but_not_online(client, admin_headers):
    drive = create_drive(client, admin_headers)
    assert (
        client.post(
            f"/api/drives/{drive['id']}/installed", headers=admin_headers
        ).status_code
        == 200
    )
    row = client.get("/api/dashboard/drives", headers=admin_headers).json()["drives"][0]
    assert row["installed"] is True
    assert row["installed_at"] is not None
    assert row["agent_seen"] is False
    assert row["online"] is False


def test_agent_check_in_makes_the_drive_online(client, admin_headers):
    drive = create_drive(client, admin_headers)
    agent = {"Authorization": f"Bearer {drive['token']}"}

    # First contact alone proves the program is installed.
    assert client.get("/api/agent/me", headers=agent).status_code == 200
    row = client.get("/api/dashboard/drives", headers=admin_headers).json()["drives"][0]
    assert row["installed"] is True
    assert row["agent_seen"] is True
    assert row["online"] is True
    assert row["last_seen_at"] is not None


def test_stale_drive_goes_offline(client, admin_headers, settings):
    drive = create_drive(client, admin_headers)
    app = client.app
    stale = time.time() - 10 * 60
    with app.state.db.connect() as conn:
        conn.execute(
            "UPDATE drives SET last_seen_at = ?, installed_at = ? WHERE id = ?",
            (stale, stale, drive["id"]),
        )
    row = client.get("/api/dashboard/drives", headers=admin_headers).json()["drives"][0]
    assert row["installed"] is True
    assert row["online"] is False


def test_revoked_drive_is_listed_but_offline(client, admin_headers):
    drive = create_drive(client, admin_headers)
    agent = {"Authorization": f"Bearer {drive['token']}"}
    client.get("/api/agent/queue", headers=agent)
    client.post(f"/api/drives/{drive['id']}/revoke", headers=admin_headers)

    row = client.get("/api/dashboard/drives", headers=admin_headers).json()["drives"][0]
    assert row["revoked"] is True
    assert row["installed"] is True
    assert row["online"] is False


def test_installed_endpoint_requires_admin_and_a_real_drive(client, admin_headers):
    drive = create_drive(client, admin_headers)
    assert client.post(f"/api/drives/{drive['id']}/installed").status_code == 401
    assert (
        client.post("/api/drives/nope/installed", headers=admin_headers).status_code
        == 404
    )
    # Reporting twice is harmless.
    assert client.post(
        f"/api/drives/{drive['id']}/installed", headers=admin_headers
    ).status_code == 200
    assert client.post(
        f"/api/drives/{drive['id']}/installed", headers=admin_headers
    ).status_code == 200


# -- per-drive actions ---------------------------------------------------

ACTION_HEADERS = {"X-Dashboard-Request": "1"}


def test_dashboard_upload_queues_files(client, admin_headers, app):
    drive = create_drive(client, admin_headers)
    login(client)

    response = client.post(
        f"/api/dashboard/drives/{drive['id']}/files",
        headers=ACTION_HEADERS,
        files={"file": ("letter.txt", b"hello from the page", "text/plain")},
        data={"note": "from dashboard"},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["filename"] == "letter.txt"
    assert record["note"] == "from dashboard"

    queue = client.get(
        "/api/agent/queue", headers={"Authorization": f"Bearer {drive['token']}"}
    ).json()
    assert [f["filename"] for f in queue["files"]] == ["letter.txt"]
    row = client.get("/api/dashboard/drives").json()["drives"][0]
    assert row["pending_count"] == 1


def test_dashboard_actions_reject_cross_site_style_requests(client, admin_headers, app):
    drive = create_drive(client, admin_headers)
    upload_url = f"/api/dashboard/drives/{drive['id']}/files"
    payload = {"file": ("csrf.txt", b"x", "text/plain")}

    # No session at all.
    assert client.post(upload_url, files=payload).status_code == 401
    login(client)
    # Session but no X-Dashboard-Request header: refused, nothing saved.
    assert client.post(upload_url, files=payload).status_code == 403
    assert client.post(
        f"/api/dashboard/drives/{drive['id']}/revoke"
    ).status_code == 403
    assert app.state.db.list_files(drive["id"]) == []


def test_dashboard_upload_enforces_safety_rules(client, admin_headers, settings):
    drive = create_drive(client, admin_headers)
    login(client)
    blocked = client.post(
        f"/api/dashboard/drives/{drive['id']}/files",
        headers=ACTION_HEADERS,
        files={"file": ("setup.exe", b"MZ", "application/octet-stream")},
    )
    assert blocked.status_code == 415
    assert list(settings.files_dir.iterdir()) == []


def test_dashboard_revoke_disables_the_agent(client, admin_headers):
    drive = create_drive(client, admin_headers)
    login(client)
    response = client.post(
        f"/api/dashboard/drives/{drive['id']}/revoke", headers=ACTION_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert client.get(
        "/api/agent/queue", headers={"Authorization": f"Bearer {drive['token']}"}
    ).status_code == 403


def test_dashboard_rotate_shows_new_token_once(client, admin_headers):
    drive = create_drive(client, admin_headers)
    login(client)
    response = client.post(
        f"/api/dashboard/drives/{drive['id']}/rotate-token", headers=ACTION_HEADERS
    )
    assert response.status_code == 200
    new_token = response.json()["token"]
    assert new_token != drive["token"]

    assert client.get(
        "/api/agent/queue", headers={"Authorization": f"Bearer {drive['token']}"}
    ).status_code == 401
    assert client.get(
        "/api/agent/queue", headers={"Authorization": f"Bearer {new_token}"}
    ).status_code == 200


def test_rotating_a_revoked_drive_is_refused(client, admin_headers):
    drive = create_drive(client, admin_headers)
    client.post(f"/api/drives/{drive['id']}/revoke", headers=admin_headers)
    login(client)
    response = client.post(
        f"/api/dashboard/drives/{drive['id']}/rotate-token", headers=ACTION_HEADERS
    )
    assert response.status_code == 409


def test_dashboard_actions_work_with_the_admin_key(client, admin_headers):
    drive = create_drive(client, admin_headers)
    assert client.post(
        f"/api/dashboard/drives/{drive['id']}/files",
        headers=admin_headers,
        files={"file": ("scripted.txt", b"x", "text/plain")},
    ).status_code == 201
    assert client.post(
        f"/api/dashboard/drives/{drive['id']}/rotate-token", headers=admin_headers
    ).status_code == 200


# -- storage -------------------------------------------------------------


def test_old_databases_are_migrated(tmp_path):
    from server.db import Database

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE drives (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            last_seen_at REAL
        );
        INSERT INTO drives VALUES ('abc', 'Old', 'hash', 1.0, 0, NULL);
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    drive = db.get_drive("abc")
    assert drive is not None
    assert drive["installed_at"] is None
    db.mark_installed("abc")
    assert db.get_drive("abc")["installed_at"] is not None
