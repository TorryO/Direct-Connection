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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "direct-connection.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

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
        )
