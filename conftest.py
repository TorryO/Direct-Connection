"""Test helpers: put the project root on sys.path and build isolated servers."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.app import create_app  # noqa: E402
from server.config import Settings  # noqa: E402

ADMIN_KEY = "test-admin-key-0123456789"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", admin_key=ADMIN_KEY)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def new_drive(client, admin_headers) -> dict:
    response = client.post("/api/drives", json={"name": "Test stick"}, headers=admin_headers)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def live_server(app):
    """Run the app under real uvicorn on a free port (for agent tests)."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
