"""Agent behaviour, exercised against a real uvicorn server."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).resolve().parent.parent / "agent" / "agent.py"


@pytest.fixture(scope="session")
def agent_module():
    spec = importlib.util.spec_from_file_location("dc_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_drive_dir(tmp_path: Path, server_url: str, token: str, agent_module) -> Path:
    drive = tmp_path / "FLASH"
    drive.mkdir()
    (drive / "Inbox").mkdir()
    (drive / agent_module.CONFIG_NAME).write_text(
        json.dumps(
            {
                "server_url": server_url,
                "drive_id": "ignored",
                "token": token,
                "inbox": "Inbox",
                "poll_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    return drive


def upload(client, headers, drive_id, name, content):
    response = client.post(
        f"/api/drives/{drive_id}/files",
        headers=headers,
        files={"file": (name, content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_config_is_required(agent_module, tmp_path):
    with pytest.raises(agent_module.AgentError):
        agent_module.load_config(tmp_path)


def test_inbox_must_stay_inside_the_drive(agent_module, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    with pytest.raises(agent_module.AgentError):
        agent_module.Agent(
            drive, {"server_url": "http://127.0.0.1:1", "token": "dc_x", "inbox": "../.."}
        )


def test_agent_delivers_files_and_marks_them_delivered(
    agent_module, client, admin_headers, new_drive, live_server, tmp_path
):
    content = b"holiday photos" * 50
    record = upload(client, admin_headers, new_drive["id"], "notes.txt", content)

    drive = make_drive_dir(tmp_path, live_server, new_drive["token"], agent_module)
    logs: list[str] = []
    agent = agent_module.Agent(drive, agent_module.load_config(drive), log=logs.append)

    assert agent.check_once() == 1

    saved = drive / "Inbox" / "notes.txt"
    assert saved.read_bytes() == content
    assert agent.sha256_of(saved) == record["sha256"]
    assert drive / "agent.py" not in list(drive.iterdir())

    auth = {"Authorization": f"Bearer {new_drive['token']}"}
    assert client.get("/api/agent/queue", headers=auth).json()["files"] == []
    listed = client.get(f"/api/drives/{new_drive['id']}/files", headers=admin_headers).json()
    assert listed[0]["delivered_at"] is not None
    assert any("SHA-256 verified" in line for line in logs)

    # Nothing new: a second poll is a no-op.
    assert agent.check_once() == 0


def test_agent_never_overwrites_existing_files(
    agent_module, client, admin_headers, new_drive, live_server, tmp_path
):
    upload(client, admin_headers, new_drive["id"], "same.txt", b"first")
    drive = make_drive_dir(tmp_path, live_server, new_drive["token"], agent_module)
    agent = agent_module.Agent(drive, agent_module.load_config(drive))

    assert agent.check_once() == 1
    upload(client, admin_headers, new_drive["id"], "same.txt", b"second")
    assert agent.check_once() == 1

    assert (drive / "Inbox" / "same.txt").read_bytes() == b"first"
    copies = sorted(p.name for p in (drive / "Inbox").iterdir())
    assert copies == ["same (1).txt", "same.txt"]
    assert (drive / "Inbox" / "same (1).txt").read_bytes() == b"second"


def test_agent_discards_corrupted_downloads(
    agent_module, client, admin_headers, new_drive, live_server, tmp_path, app
):
    record = upload(client, admin_headers, new_drive["id"], "report.txt", b"original")
    stored = app.state.db.get_file(record["id"])
    app.state.storage.path_for(stored["storage_key"]).write_bytes(b"tampered")

    drive = make_drive_dir(tmp_path, live_server, new_drive["token"], agent_module)
    logs: list[str] = []
    agent = agent_module.Agent(drive, agent_module.load_config(drive), log=logs.append)

    assert agent.check_once() == 0
    assert not (drive / "Inbox" / "report.txt").exists()
    assert not list((drive / "Inbox").glob(".dcpart-*"))
    assert any("checksum mismatch" in line for line in logs)

    auth = {"Authorization": f"Bearer {new_drive['token']}"}
    assert len(client.get("/api/agent/queue", headers=auth).json()["files"]) == 1


def test_agent_refuses_executables_client_side(agent_module, tmp_path):
    drive = make_drive_dir(tmp_path, "http://127.0.0.1:1", "dc_token", agent_module)
    agent = agent_module.Agent(drive, agent_module.load_config(drive))
    payload = hashlib.sha256(b"MZ").hexdigest()

    agent._get_json = lambda path: {  # type: ignore[method-assign]
        "files": [{"id": "1", "filename": "setup.exe", "sha256": payload, "size": 2}]
    }
    assert agent.check_once() == 0
    assert list((drive / "Inbox").iterdir()) == []


def test_agent_reports_revoked_drive(
    agent_module, client, admin_headers, new_drive, live_server, tmp_path
):
    drive = make_drive_dir(tmp_path, live_server, new_drive["token"], agent_module)
    logs: list[str] = []
    agent = agent_module.Agent(drive, agent_module.load_config(drive), log=logs.append)
    client.post(f"/api/drives/{new_drive['id']}/revoke", headers=admin_headers)

    with pytest.raises(agent_module.AgentError):
        agent.check_once()
    assert agent.revoked is True
    assert any("revoked" in line for line in logs)


def test_safe_name_strips_paths(agent_module):
    assert agent_module.safe_name("../../etc/passwd") == "passwd"
    assert agent_module.safe_name("C:\\Windows\\evil.txt") == "evil.txt"
    with pytest.raises(agent_module.AgentError):
        agent_module.safe_name("..")
