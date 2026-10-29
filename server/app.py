"""Direct-Connection server: drive registry, uploads and the agent download queue.

Auth model
----------
* Owner  -> ``X-Admin-Key`` header (the DC_ADMIN_KEY environment variable).
* Agent  -> ``Authorization: Bearer <drive token>``. Only the SHA-256 of each
  drive token is stored, and the owner can revoke or rotate it at any time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .storage import FileRejected, FileStorage, check_allowed, sanitize_filename

NOTE_MAX_LENGTH = 2000
QUEUE_LIMIT = 100


class DriveCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return "dc_" + secrets.token_urlsafe(32)


def _drive_public(drive: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": drive["id"],
        "name": drive["name"],
        "created_at": _iso(drive["created_at"]),
        "revoked": bool(drive["revoked"]),
        "last_seen_at": _iso(drive.get("last_seen_at")),
        "pending_count": drive.get("pending_count"),
    }


def _file_public(file: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": file["id"],
        "filename": file["filename"],
        "size": file["size"],
        "sha256": file["sha256"],
        "note": file["note"],
        "created_at": _iso(file["created_at"]),
        "delivered_at": _iso(file["delivered_at"]),
    }


def create_app(settings: Settings) -> FastAPI:
    db = Database(settings.db_path)
    storage = FileStorage(settings.files_dir)

    app = FastAPI(
        title="Direct-Connection",
        version="0.1.0",
        description="Uploads for a USB drive, pulled down by the drive's own agent.",
    )
    app.state.settings = settings
    app.state.db = db
    app.state.storage = storage

    # -- auth dependencies ---------------------------------------------

    def require_admin(
        x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
    ) -> None:
        if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid admin key.",
            )

    def require_drive(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing drive token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        drive = db.get_drive_by_token_hash(_hash_token(token.strip()))
        if drive is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unknown drive token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if drive["revoked"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This drive has been revoked by its owner.",
            )
        return drive

    def owned_drive(drive_id: str) -> dict[str, Any]:
        drive = db.get_drive(drive_id)
        if drive is None:
            raise HTTPException(status_code=404, detail="Drive not found.")
        return drive

    # -- owner endpoints -----------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/drives", dependencies=[Depends(require_admin)], status_code=201)
    def create_drive(payload: DriveCreate) -> dict[str, Any]:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Drive name is required.")
        token = _new_token()
        drive = db.create_drive(uuid.uuid4().hex, name, _hash_token(token))
        # The token is shown exactly once; only its hash is kept.
        return {**_drive_public({**drive, "pending_count": 0}), "token": token}

    @app.get("/api/drives", dependencies=[Depends(require_admin)])
    def list_drives() -> list[dict[str, Any]]:
        return [_drive_public(drive) for drive in db.list_drives()]

    @app.get("/api/drives/{drive_id}", dependencies=[Depends(require_admin)])
    def get_drive(drive_id: str) -> dict[str, Any]:
        return _drive_public(owned_drive(drive_id))

    @app.post(
        "/api/drives/{drive_id}/revoke", dependencies=[Depends(require_admin)]
    )
    def revoke_drive(drive_id: str) -> dict[str, Any]:
        owned_drive(drive_id)
        db.revoke_drive(drive_id)
        return _drive_public(owned_drive(drive_id))

    @app.post(
        "/api/drives/{drive_id}/rotate-token", dependencies=[Depends(require_admin)]
    )
    def rotate_token(drive_id: str) -> dict[str, Any]:
        owned_drive(drive_id)
        token = _new_token()
        db.set_token_hash(drive_id, _hash_token(token))
        return {**_drive_public(owned_drive(drive_id)), "token": token}

    @app.post(
        "/api/drives/{drive_id}/files",
        dependencies=[Depends(require_admin)],
        status_code=201,
    )
    async def upload_file(
        drive_id: str,
        file: UploadFile,
        note: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        owned_drive(drive_id)
        try:
            filename = sanitize_filename(file.filename or "")
            check_allowed(filename, settings.blocked_extensions)
            storage_key, size, sha256 = storage.save(
                file.file, settings.max_file_bytes
            )
        except FileRejected as exc:
            # 413 payload-too-large vs 415 blocked/missing name.
            code = 413 if "larger than" in str(exc) else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        finally:
            await file.close()

        clean_note = (note or "").strip() or None
        if clean_note and len(clean_note) > NOTE_MAX_LENGTH:
            storage.delete(storage_key)
            raise HTTPException(
                status_code=422, detail=f"Note is longer than {NOTE_MAX_LENGTH} characters."
            )

        record = db.add_file(
            uuid.uuid4().hex, drive_id, filename, size, sha256, clean_note, storage_key
        )
        return _file_public(record)

    @app.get("/api/drives/{drive_id}/files", dependencies=[Depends(require_admin)])
    def list_drive_files(drive_id: str) -> list[dict[str, Any]]:
        owned_drive(drive_id)
        return [_file_public(f) for f in db.list_files(drive_id)]

    @app.delete(
        "/api/drives/{drive_id}/files/{file_id}", dependencies=[Depends(require_admin)]
    )
    def delete_drive_file(drive_id: str, file_id: str) -> dict[str, bool]:
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive_id:
            raise HTTPException(status_code=404, detail="File not found.")
        storage.delete(file["storage_key"])
        db.delete_file(file_id)
        return {"deleted": True}

    # -- agent endpoints -----------------------------------------------

    @app.get("/api/agent/me")
    def agent_me(drive: dict[str, Any] = Depends(require_drive)) -> dict[str, Any]:
        return {
            "id": drive["id"],
            "name": drive["name"],
            "revoked": bool(drive["revoked"]),
            "created_at": _iso(drive["created_at"]),
        }

    @app.get("/api/agent/queue")
    def agent_queue(
        drive: dict[str, Any] = Depends(require_drive),
    ) -> dict[str, Any]:
        db.touch_drive(drive["id"])
        files = db.pending_files(drive["id"], limit=QUEUE_LIMIT)
        return {"drive_id": drive["id"], "files": [_file_public(f) for f in files]}

    @app.get("/api/agent/files/{file_id}")
    def agent_download(
        file_id: str,
        request: Request,
        drive: dict[str, Any] = Depends(require_drive),
    ) -> FileResponse:
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive["id"]:
            raise HTTPException(status_code=404, detail="File not found.")
        path = storage.path_for(file["storage_key"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="Stored file is missing.")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={
                "X-Content-Sha256": file["sha256"],
                "X-File-Name": file["filename"].encode("utf-8").hex(),
                "Cache-Control": "no-store",
            },
            filename=file["filename"],
        )

    @app.post("/api/agent/files/{file_id}/ack")
    def agent_ack(
        file_id: str,
        drive: dict[str, Any] = Depends(require_drive),
        sha256: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        """Confirm delivery. The hash the agent actually wrote must match ours."""
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive["id"]:
            raise HTTPException(status_code=404, detail="File not found.")
        if sha256.strip().lower() != file["sha256"]:
            raise HTTPException(
                status_code=422, detail="Checksum mismatch; file not marked delivered."
            )
        db.mark_delivered(file_id)
        return {"delivered": True, "id": file_id}

    return app


def settings_from_env() -> Settings:
    return Settings.from_env()


def data_dir() -> Path:
    """Kept for callers that only need the default data location."""
    return Path("./data")
