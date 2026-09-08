"""Serve the built frontend from the API's own origin.

Used by the macOS app bundle, where `vite build` output ships inside the bundle and
there is no dev server. Making the frontend and the API same-origin removes three
things at once: CORS, the Vite proxy, and the `vite.config.ts` env-var trap that made
every `/api` call fail on a non-default port (`AGENTS.md` caveat #9).

Off by default. Only `FINANCE_APP_WEB_DIR` turns it on, so the container and local dev
flows keep using Vite.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

WEB_DIR_ENV_VAR = "FINANCE_APP_WEB_DIR"


def configured_web_directory() -> Path | None:
    """The built frontend's directory, or ``None`` if not configured or not usable."""
    raw = os.environ.get(WEB_DIR_ENV_VAR)
    if not raw:
        return None
    directory = Path(raw)
    # Require index.html rather than just the directory. An empty or half-copied
    # directory would otherwise mount successfully and serve 404s for everything,
    # which reads as a broken app rather than a missing build.
    if not (directory / "index.html").is_file():
        return None
    return directory


def index_file() -> Path | None:
    directory = configured_web_directory()
    return directory / "index.html" if directory else None


def mount_static_frontend(app) -> Path | None:
    """Mount the built frontend at ``/``. Returns the directory, or ``None``.

    Must be called *after* every router and route is registered. Starlette matches in
    registration order, so a mount at ``/`` added earlier would shadow the API.
    """
    directory = configured_web_directory()
    if directory is None:
        return None
    # html=True serves index.html for a directory request.
    app.mount("/", StaticFiles(directory=directory, html=True), name="web")
    return directory
