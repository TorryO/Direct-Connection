"""Run the API with `python -m server`."""

from __future__ import annotations

import os
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, Settings


def main() -> int:
    host = os.environ.get("DC_HOST", "127.0.0.1")
    port = int(os.environ.get("DC_PORT", "8000"))
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    app = create_app(settings)
    print(f"Direct-Connection server on http://{host}:{port} (data: {settings.data_dir})")
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
