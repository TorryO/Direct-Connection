"""Persistence for drives and their files.

SQLite is the default so local development and tests need no services. Set
``DC_DATABASE_URL`` to a Postgres DSN when running on a serverless platform,
where the local filesystem does not survive between requests.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_SQLITE = """
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
    delivered_at REAL,
    uploaded_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_files_pending ON files(drive_id, delivered_at);
"""

SCHEMA_POSTGRES = (
    """
    CREATE TABLE IF NOT EXISTS drives (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   DOUBLE PRECISION NOT NULL,
        revoked      INTEGER NOT NULL DEFAULT 0,
        last_seen_at DOUBLE PRECISION,
        installed_at DOUBLE PRECISION
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id           TEXT PRIMARY KEY,
        drive_id     TEXT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
        filename     TEXT NOT NULL,
        size         BIGINT NOT NULL,
        sha256       TEXT NOT NULL,
        note         TEXT,
        storage_key  TEXT NOT NULL UNIQUE,
        created_at   DOUBLE PRECISION NOT NULL,
        delivered_at DOUBLE PRECISION,
        uploaded_at  DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_pending ON files(drive_id, delivered_at)",
)


class _PostgresConnection:
    """Adapter so the rest of the module can keep the sqlite3 call style."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self._conn.execute(sql.replace("?", "%s"), params)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self._conn.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class Database:
    def __init__(self, path: Path | None = None, url: str | None = None) -> None:
        if url:
            self.url: str | None = url
            self.path: Path | None = None
            self.is_postgres = True
            self._init_postgres()
        elif path is not None:
            self.url = None
            self.path = Path(path)
            self.is_postgres = False
            self._init_sqlite()
        else:
            raise ValueError("Database needs a path or a url")

    # -- setup -----------------------------------------------------------

    def _init_sqlite(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQLITE)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(drives)").fetchall()
            }
            if "installed_at" not in columns:
                conn.execute("ALTER TABLE drives ADD COLUMN installed_at REAL")
            file_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(files)").fetchall()
            }
            if "uploaded_at" not in file_columns:
                conn.execute("ALTER TABLE files ADD COLUMN uploaded_at REAL")
                # Everything that existed before this column came from a full upload.
                conn.execute("UPDATE files SET uploaded_at = created_at")

    def _init_postgres(self) -> None:
        import psycopg

        with self.connect() as conn:
            for statement in SCHEMA_POSTGRES:
                conn.execute(statement)
            # Tables created by an earlier release may predate uploaded_at.
            conn.execute(
                """
                ALTER TABLE files ADD COLUMN IF NOT EXISTS uploaded_at DOUBLE PRECISION
                """
            )

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            conn = _PostgresConnection(
                psycopg.connect(self.url, row_factory=dict_row)
            )
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return

        sqlite_conn = sqlite3.connect(self.path, timeout=30.0)
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_conn.execute("PRAGMA foreign_keys = ON")
        sqlite_conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield sqlite_conn
            sqlite_conn.commit()
        except Exception:
            sqlite_conn.rollback()
            raise
        finally:
            sqlite_conn.close()

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
                    WHERE f.drive_id = d.id
                      AND f.delivered_at IS NULL
                      AND f.uploaded_at IS NOT NULL
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
        """Record a file that is already stored (direct multipart upload)."""
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO files
                    (id, drive_id, filename, size, sha256, note, storage_key,
                     created_at, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, drive_id, filename, size, sha256, note, storage_key, now, now),
            )
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        assert row is not None
        return dict(row)

    def create_upload_intent(
        self,
        file_id: str,
        drive_id: str,
        filename: str,
        size: int,
        note: str | None,
        storage_key: str,
    ) -> dict[str, Any]:
        """Reserve a row before the client uploads the bytes to storage."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO files
                    (id, drive_id, filename, size, sha256, note, storage_key,
                     created_at, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, drive_id, filename, size, "", note, storage_key,
                 time.time(), None),
            )
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        assert row is not None
        return dict(row)

    def mark_uploaded(self, file_id: str, sha256: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE files SET sha256 = ?, uploaded_at = ? WHERE id = ?",
                (sha256, time.time(), file_id),
            )

    def list_files(self, drive_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM files
                WHERE drive_id = ? AND uploaded_at IS NOT NULL
                ORDER BY created_at DESC
                """,
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
                WHERE drive_id = ? AND delivered_at IS NULL AND uploaded_at IS NOT NULL
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


def open_database(path: Path | str, url: str | None = None) -> Database:
    if url:
        return Database(url=url)
    return Database(path=Path(path))
