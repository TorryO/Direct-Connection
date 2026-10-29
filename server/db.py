"""SQLite persistence for drives and their pending files."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    last_seen_at REAL,
    installed_at REAL
);

CREATE TABLE IF NOT EXISTS files (
    id           TEXT PRIMARY KEY,
    drive_id     TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    note         TEXT,
    storage_key  TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL,
    delivered_at REAL
);

CREATE INDEX IF NOT EXISTS idx_files_pending ON files(drive_id, delivered_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Older databases predate the installed_at column.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(drives)").fetchall()
            }
            if "installed_at" not in columns:
                conn.execute("ALTER TABLE drives ADD COLUMN installed_at REAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- drives ---------------------------------------------------------

    def create_drive(self, drive_id: str, name: str, token_hash: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO drives (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (drive_id, name, token_hash, time.time()),
            )
        drive = self.get_drive(drive_id)
        assert drive is not None
        return drive

    def get_drive(self, drive_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM drives WHERE id = ?", (drive_id,)).fetchone()
        return dict(row) if row else None

    def get_drive_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM drives WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def list_drives(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, (
                    SELECT COUNT(*) FROM files f
                    WHERE f.drive_id = d.id AND f.delivered_at IS NULL
                ) AS pending_count
                FROM drives d
                ORDER BY d.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_token_hash(self, drive_id: str, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE drives SET token_hash = ? WHERE id = ?", (token_hash, drive_id)
            )

    def revoke_drive(self, drive_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE drives SET revoked = 1 WHERE id = ?", (drive_id,))

    def touch_drive(self, drive_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE drives SET last_seen_at = ? WHERE id = ?", (time.time(), drive_id)
            )

    def mark_installed(self, drive_id: str) -> None:
        """Record the first time the agent was written to the drive."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE drives SET installed_at = COALESCE(installed_at, ?) WHERE id = ?",
                (time.time(), drive_id),
            )

    # -- files ----------------------------------------------------------

    def add_file(
        self,
        file_id: str,
        drive_id: str,
        filename: str,
        size: int,
        sha256: str,
        note: str | None,
        storage_key: str,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO files
                    (id, drive_id, filename, size, sha256, note, storage_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, drive_id, filename, size, sha256, note, storage_key, time.time()),
            )
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        assert row is not None
        return dict(row)

    def list_files(self, drive_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE drive_id = ? ORDER BY created_at DESC",
                (drive_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def pending_files(self, drive_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM files
                WHERE drive_id = ? AND delivered_at IS NULL
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (drive_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_delivered(self, file_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE files SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                (time.time(), file_id),
            )

    def delete_file(self, file_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
