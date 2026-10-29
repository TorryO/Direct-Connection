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
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Settings
from .dashboard import render_dashboard, render_drive, render_login
from .db import open_database
from .storage import (
    URL_TTL_SECONDS,
    FileRejected,
    LocalStorage,
    check_allowed,
    open_storage,
    sanitize_filename,
)

NOTE_MAX_LENGTH = 2000
QUEUE_LIMIT = 100

# The dashboard calls a drive "online" when its agent has checked in this recently.
# Agents poll every 30s by default, so this tolerates a missed poll or two.
ONLINE_WINDOW_SECONDS = 90

SESSION_COOKIE = "dc_session"
SESSION_TTL_SECONDS = 12 * 60 * 60

_STORAGE_KEY = re.compile(r"^[0-9a-f]{32}$")


class DriveCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class UploadIntent(BaseModel):
    filename: str = Field(min_length=1, max_length=400)
    size: int = Field(gt=0)
    note: str | None = None


class UploadComplete(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


def _cookie_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return forwarded == "https"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return "dc_" + secrets.token_urlsafe(32)


def _sign_session(secret: str, issued: int) -> str:
    message = f"dc-dashboard-session:{issued}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _new_session(secret: str) -> str:
    issued = int(time.time())
    return f"{issued}.{_sign_session(secret, issued)}"


def _session_valid(secret: str, value: str | None) -> bool:
    if not value:
        return False
    issued_raw, _, signature = value.partition(".")
    try:
        issued = int(issued_raw)
    except ValueError:
        return False
    if time.time() - issued > SESSION_TTL_SECONDS:
        return False
    return hmac.compare_digest(signature, _sign_session(secret, issued))


def _drive_public(drive: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": drive["id"],
        "name": drive["name"],
        "created_at": _iso(drive["created_at"]),
        "revoked": bool(drive["revoked"]),
        "last_seen_at": _iso(drive.get("last_seen_at")),
        "installed_at": _iso(drive.get("installed_at")),
        "pending_count": drive.get("pending_count"),
    }


def _dashboard_drive(drive: dict[str, Any], now: float) -> dict[str, Any]:
    """A drive row for the overview page: public fields plus vague status."""
    last_seen = drive.get("last_seen_at")
    installed_at = drive.get("installed_at")
    revoked = bool(drive["revoked"])
    return {
        **_drive_public(drive),
        "online": bool(
            not revoked and last_seen is not None and now - last_seen <= ONLINE_WINDOW_SECONDS
        ),
        # Provisioned by the CLI, or the agent has connected at least once.
        "installed": installed_at is not None or last_seen is not None,
        "agent_seen": last_seen is not None,
        "pending_count": drive.get("pending_count") or 0,
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
    db = open_database(settings.db_path, settings.database_url)
    storage = open_storage(settings)

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

    def require_owner(
        request: Request,
        x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
    ) -> None:
        """Dashboard API auth: a signed session cookie, or the admin key."""
        if x_admin_key and hmac.compare_digest(x_admin_key, settings.admin_key):
            return
        if _session_valid(
            settings.admin_key, request.cookies.get(SESSION_COOKIE)
        ):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid dashboard session.",
        )

    def require_owner_action(
        request: Request,
        x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
        x_dashboard_request: Annotated[
            str | None, Header(alias="X-Dashboard-Request")
        ] = None,
    ) -> None:
        """Auth for state-changing dashboard calls.

        The admin key is enough for scripts. A session cookie only counts when
        the page also sends ``X-Dashboard-Request``, which a cross-site form or
        fetch cannot add without a CORS preflight this server never grants.
        """
        if x_admin_key and hmac.compare_digest(x_admin_key, settings.admin_key):
            return
        if _session_valid(settings.admin_key, request.cookies.get(SESSION_COOKIE)):
            if x_dashboard_request == "1":
                return
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing X-Dashboard-Request header.",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid dashboard session.",
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

    # -- signed blob endpoints (local storage only) ----------------------

    def _local_storage() -> LocalStorage | None:
        return storage if isinstance(storage, LocalStorage) else None

    @app.put("/api/blob/upload/{key}")
    async def blob_upload(
        key: str,
        request: Request,
        expires: int = 0,
        signature: str = "",
    ) -> dict[str, Any]:
        local = _local_storage()
        if local is None or not _STORAGE_KEY.match(key):
            raise HTTPException(status_code=404, detail="Not found.")
        if not local.verify("upload", key, expires, signature):
            raise HTTPException(status_code=403, detail="Upload link is invalid or expired.")
        raw_length = request.headers.get("content-length")
        if raw_length and raw_length.isdigit() and int(raw_length) > settings.max_file_bytes:
            raise HTTPException(status_code=413, detail="File is larger than the limit.")
        body = await request.body()
        try:
            size, sha256 = local.save_at(key, BytesIO(body), settings.max_file_bytes)
        except FileRejected as exc:
            code = 413 if "larger than" in str(exc) else 415
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return {"size": size, "sha256": sha256}

    @app.get("/api/blob/download/{key}")
    def blob_download(key: str, expires: int = 0, signature: str = "") -> FileResponse:
        local = _local_storage()
        if local is None or not _STORAGE_KEY.match(key):
            raise HTTPException(status_code=404, detail="Not found.")
        if not local.verify("download", key, expires, signature):
            raise HTTPException(status_code=403, detail="Download link is invalid or expired.")
        path = local.path_for(key)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Stored file is missing.")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    # -- dashboard -------------------------------------------------------

    def _no_store(response: HTMLResponse) -> HTMLResponse:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page(request: Request) -> HTMLResponse:
        if settings.dashboard_password is None:
            return _no_store(HTMLResponse(render_login(enabled=False), status_code=503))
        if _session_valid(settings.admin_key, request.cookies.get(SESSION_COOKIE)):
            return _no_store(HTMLResponse(render_dashboard()))
        return _no_store(HTMLResponse(render_login()))

    @app.post("/dashboard/login", response_class=HTMLResponse)
    def dashboard_login(
        request: Request, password: Annotated[str, Form()] = ""
    ) -> Any:
        if settings.dashboard_password is None:
            return _no_store(HTMLResponse(render_login(enabled=False), status_code=503))
        if not hmac.compare_digest(password, settings.dashboard_password):
            return _no_store(
                HTMLResponse(render_login("Wrong password."), status_code=401)
            )
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            _new_session(settings.admin_key),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )
        return response

    @app.post("/dashboard/logout")
    def dashboard_logout() -> RedirectResponse:
        response = RedirectResponse("/dashboard", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/dashboard/drives", dependencies=[Depends(require_owner)])
    def dashboard_drives() -> dict[str, Any]:
        now = time.time()
        return {
            "online_window_seconds": ONLINE_WINDOW_SECONDS,
            "drives": [_dashboard_drive(d, now) for d in db.list_drives()],
        }

    @app.get("/dashboard/drives/{drive_id}", response_class=HTMLResponse)
    def dashboard_drive_page(drive_id: str, request: Request) -> Any:
        if settings.dashboard_password is None:
            return _no_store(HTMLResponse(render_login(enabled=False), status_code=503))
        if not _session_valid(settings.admin_key, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/dashboard", status_code=303)
        drive = db.get_drive(drive_id)
        if drive is None:
            return _no_store(
                HTMLResponse("<h1>Drive not found</h1><p><a href='/dashboard'>Back</a></p>",
                             status_code=404)
            )
        return _no_store(HTMLResponse(render_drive(drive)))

    @app.get("/api/dashboard/drives/{drive_id}", dependencies=[Depends(require_owner)])
    def dashboard_drive(drive_id: str) -> dict[str, Any]:
        drive = owned_drive(drive_id)
        return {
            **_dashboard_drive(drive, time.time()),
            "files": [_file_public(f) for f in db.list_files(drive_id)],
        }

    def _delete_file(drive_id: str, file_id: str) -> dict[str, bool]:
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive_id:
            raise HTTPException(status_code=404, detail="File not found.")
        storage.delete(file["storage_key"])
        db.delete_file(file_id)
        return {"deleted": True}

    @app.delete(
        "/api/dashboard/drives/{drive_id}/files/{file_id}",
        dependencies=[Depends(require_owner_action)],
    )
    def dashboard_delete_file(drive_id: str, file_id: str) -> dict[str, bool]:
        return _delete_file(drive_id, file_id)

    async def _save_upload(
        drive_id: str, file: UploadFile, note: str | None
    ) -> dict[str, Any]:
        """Validate, store and queue one upload for a drive."""
        owned_drive(drive_id)
        try:
            filename = sanitize_filename(file.filename or "")
            check_allowed(filename, settings.blocked_extensions)
            storage_key, size, sha256 = storage.save(
                file.file, settings.max_file_bytes
            )
        except FileRejected as exc:
            # 413 payload-too-large vs 415 blocked/missing name.
            code = (
                413
                if "larger than" in str(exc)
                else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            )
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        finally:
            await file.close()

        clean_note = (note or "").strip() or None
        if clean_note and len(clean_note) > NOTE_MAX_LENGTH:
            storage.delete(storage_key)
            raise HTTPException(
                status_code=422,
                detail=f"Note is longer than {NOTE_MAX_LENGTH} characters.",
            )

        record = db.add_file(
            uuid.uuid4().hex, drive_id, filename, size, sha256, clean_note, storage_key
        )
        return _file_public(record)

    @app.post(
        "/api/dashboard/drives/{drive_id}/files",
        dependencies=[Depends(require_owner_action)],
        status_code=201,
    )
    async def dashboard_upload_file(
        drive_id: str,
        file: UploadFile,
        note: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        return await _save_upload(drive_id, file, note)

    @app.post(
        "/api/dashboard/drives/{drive_id}/revoke",
        dependencies=[Depends(require_owner_action)],
    )
    def dashboard_revoke_drive(drive_id: str) -> dict[str, Any]:
        owned_drive(drive_id)
        db.revoke_drive(drive_id)
        return _drive_public(owned_drive(drive_id))

    @app.post(
        "/api/dashboard/drives/{drive_id}/rotate-token",
        dependencies=[Depends(require_owner_action)],
    )
    def dashboard_rotate_token(drive_id: str) -> dict[str, Any]:
        drive = owned_drive(drive_id)
        if drive["revoked"]:
            raise HTTPException(
                status_code=409, detail="This drive has been revoked."
            )
        token = _new_token()
        db.set_token_hash(drive_id, _hash_token(token))
        return {**_drive_public(owned_drive(drive_id)), "token": token}

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
        "/api/drives/{drive_id}/installed", dependencies=[Depends(require_admin)]
    )
    def mark_drive_installed(drive_id: str) -> dict[str, Any]:
        """Called by the owner CLI once the agent files are on the drive."""
        owned_drive(drive_id)
        db.mark_installed(drive_id)
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
        return await _save_upload(drive_id, file, note)

    @app.get("/api/drives/{drive_id}/files", dependencies=[Depends(require_admin)])
    def list_drive_files(drive_id: str) -> list[dict[str, Any]]:
        owned_drive(drive_id)
        return [_file_public(f) for f in db.list_files(drive_id)]

    @app.delete(
        "/api/drives/{drive_id}/files/{file_id}", dependencies=[Depends(require_admin)]
    )
    def delete_drive_file(drive_id: str, file_id: str) -> dict[str, bool]:
        return _delete_file(drive_id, file_id)

    @app.post(
        "/api/drives/{drive_id}/files/upload-url",
        dependencies=[Depends(require_owner_action)],
        status_code=201,
    )
    def create_upload_url(
        drive_id: str, payload: UploadIntent
    ) -> dict[str, Any]:
        """Reserve a file and return a short-lived URL the client uploads to.

        Bytes go straight to storage, so uploads are not capped by the API's
        request-size limit (Vercel Functions stop at 4.5 MB).
        """
        owned_drive(drive_id)
        try:
            filename = sanitize_filename(payload.filename)
            check_allowed(filename, settings.blocked_extensions)
        except FileRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
            ) from exc
        if payload.size > settings.max_file_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File is larger than the {settings.max_file_bytes} byte limit.",
            )
        clean_note = (payload.note or "").strip() or None
        if clean_note and len(clean_note) > NOTE_MAX_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"Note is longer than {NOTE_MAX_LENGTH} characters.",
            )

        storage_key = uuid.uuid4().hex
        record = db.create_upload_intent(
            uuid.uuid4().hex,
            drive_id,
            filename,
            payload.size,
            clean_note,
            storage_key,
        )
        return {
            "id": record["id"],
            "filename": filename,
            "size": payload.size,
            "upload_url": storage.upload_url(storage_key),
            "expires_in": URL_TTL_SECONDS,
        }

    @app.post(
        "/api/drives/{drive_id}/files/{file_id}/complete",
        dependencies=[Depends(require_owner_action)],
    )
    def complete_upload(
        drive_id: str, file_id: str, payload: UploadComplete
    ) -> dict[str, Any]:
        owned_drive(drive_id)
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive_id:
            raise HTTPException(status_code=404, detail="File not found.")
        if file.get("uploaded_at") is not None:
            return _file_public(file)

        try:
            exists, stored_size = storage.head(file["storage_key"])
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Storage error: {exc}"
            ) from exc
        if not exists:
            db.delete_file(file_id)
            raise HTTPException(
                status_code=409, detail="Upload not found in storage; request a new URL."
            )
        if stored_size != file["size"]:
            storage.delete(file["storage_key"])
            db.delete_file(file_id)
            raise HTTPException(
                status_code=422,
                detail=f"Uploaded {stored_size} bytes but {file['size']} were expected.",
            )

        db.mark_uploaded(file_id, payload.sha256.lower())
        return _file_public(db.get_file(file_id) or file)

    # -- agent endpoints -----------------------------------------------

    @app.get("/api/agent/me")
    def agent_me(drive: dict[str, Any] = Depends(require_drive)) -> dict[str, Any]:
        db.touch_drive(drive["id"])
        db.mark_installed(drive["id"])
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
        db.mark_installed(drive["id"])
        files = db.pending_files(drive["id"], limit=QUEUE_LIMIT)
        return {"drive_id": drive["id"], "files": [_file_public(f) for f in files]}

    @app.get("/api/agent/files/{file_id}/link")
    def agent_file_link(
        file_id: str,
        drive: dict[str, Any] = Depends(require_drive),
    ) -> dict[str, Any]:
        """A short-lived URL the agent downloads from without credentials."""
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive["id"]:
            raise HTTPException(status_code=404, detail="File not found.")
        if file.get("uploaded_at") is None:
            raise HTTPException(status_code=409, detail="Upload is not complete yet.")
        return {
            "id": file["id"],
            "filename": file["filename"],
            "size": file["size"],
            "sha256": file["sha256"],
            "url": storage.download_url(file["storage_key"]),
            "expires_in": URL_TTL_SECONDS,
        }

    @app.get("/api/agent/files/{file_id}")
    def agent_download(
        file_id: str,
        request: Request,
        drive: dict[str, Any] = Depends(require_drive),
    ) -> Any:
        file = db.get_file(file_id)
        if file is None or file["drive_id"] != drive["id"]:
            raise HTTPException(status_code=404, detail="File not found.")
        if file.get("uploaded_at") is None:
            raise HTTPException(status_code=409, detail="Upload is not complete yet.")
        if not isinstance(storage, LocalStorage):
            return RedirectResponse(
                storage.download_url(file["storage_key"]),
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Cache-Control": "no-store"},
            )
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
