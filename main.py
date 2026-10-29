"""Vercel entrypoint: Vercel looks for a FastAPI instance named ``app``.

Locally the same app runs with ``python -m server``; this module only exists so
the platform can import it. Configuration comes from the same environment
variables (see README).
"""

from __future__ import annotations

from server.app import create_app
from server.config import Settings

app = create_app(Settings.from_env())
