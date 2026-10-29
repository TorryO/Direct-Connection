"""Server configuration, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB

# Files the agent must never accept. The server refuses them too so a bad upload
# is caught early, and the agent refuses them again on its own.
BLOCKED_EXTENSIONS = frozenset(
    {
        "app", "apk", "bat", "bash", "bin", "chm", "cmd", "com", "cpl", "deb",
        "dll", "dmg", "exe", "gadget", "hta", "img", "iso", "jar", "js", "jse",
        "lnk", "msi", "pif", "pkg", "ps1", "psm1", "rpm", "scr", "sh", "url",
        "vbe", "vbs", "wsf", "wsh", "zsh",
    }
)


class ConfigError(RuntimeError):
    """Raised when the server is not configured safely enough to start."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_key: str
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    blocked_extensions: frozenset[str] = field(default=BLOCKED_EXTENSIONS)
    dashboard_password: str | None = None
    # Postgres DSN. When unset the server uses SQLite under data_dir.
    database_url: str | None = None
    # S3-compatible object storage. When bucket is set, files live there
    # instead of under data_dir/files.
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_prefix: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "direct-connection.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def uses_object_storage(self) -> bool:
        return bool(self.s3_bucket)

    @classmethod
    def from_env(cls) -> "Settings":
        admin_key = os.environ.get("DC_ADMIN_KEY", "").strip()
        if not admin_key:
            raise ConfigError(
                "DC_ADMIN_KEY is not set. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
                "then export DC_ADMIN_KEY=<value> before starting the server."
            )
        if len(admin_key) < 16:
            raise ConfigError("DC_ADMIN_KEY must be at least 16 characters long.")

        dashboard_password = os.environ.get("DC_DASHBOARD_PASSWORD", "").strip() or None
        if dashboard_password is not None and len(dashboard_password) < 16:
            raise ConfigError(
                "DC_DASHBOARD_PASSWORD must be at least 16 characters long."
            )

        database_url = os.environ.get("DC_DATABASE_URL", "").strip() or None
        if database_url and not database_url.startswith(
            ("postgres://", "postgresql://")
        ):
            raise ConfigError("DC_DATABASE_URL must be a postgres:// or postgresql:// URL.")

        s3_bucket = os.environ.get("DC_S3_BUCKET", "").strip() or None
        s3_access_key_id = os.environ.get("DC_S3_ACCESS_KEY_ID", "").strip() or None
        s3_secret_access_key = (
            os.environ.get("DC_S3_SECRET_ACCESS_KEY", "").strip() or None
        )
        if s3_bucket and not (s3_access_key_id and s3_secret_access_key):
            raise ConfigError(
                "DC_S3_BUCKET needs DC_S3_ACCESS_KEY_ID and DC_S3_SECRET_ACCESS_KEY."
            )

        raw_size = os.environ.get("DC_MAX_FILE_BYTES", "").strip()
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
        if raw_size:
            try:
                max_file_bytes = int(raw_size)
            except ValueError as exc:
                raise ConfigError("DC_MAX_FILE_BYTES must be an integer.") from exc
            if max_file_bytes <= 0:
                raise ConfigError("DC_MAX_FILE_BYTES must be positive.")

        return cls(
            data_dir=Path(os.environ.get("DC_DATA_DIR", "./data")),
            admin_key=admin_key,
            max_file_bytes=max_file_bytes,
            dashboard_password=dashboard_password,
            database_url=database_url,
            s3_bucket=s3_bucket,
            s3_endpoint_url=os.environ.get("DC_S3_ENDPOINT_URL", "").strip() or None,
            s3_region=os.environ.get("DC_S3_REGION", "").strip() or "auto",
            s3_access_key_id=s3_access_key_id,
            s3_secret_access_key=s3_secret_access_key,
            s3_prefix=os.environ.get("DC_S3_PREFIX", "").strip().strip("/"),
        )
