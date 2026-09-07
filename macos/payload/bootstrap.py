"""Entry point for the bundled backend. Invoked as `python -I bootstrap.py`.

Why sys.path is built here rather than via PYTHONPATH: `-I` implies `-E`, so the
interpreter ignores every PYTHON* environment variable, PYTHONPATH and PYTHONHOME
included. That is exactly the isolation we want -- a Homebrew Python, a stray
PYTHONPATH, or a `~/.local/lib/python3.12/site-packages` must not be able to
shadow a bundled package -- but it means the search path has to be set in code.

Layout this expects (see macos/scripts/build_backend.sh):

    backend/
      bootstrap.py     <- this file
      python/          <- relocatable CPython
      site-packages/   <- wheels installed with --target
      app/             <- src/, rules/, plugins/ copied verbatim
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE / "app"
SITE_PACKAGES = HERE / "site-packages"


def _configure_path() -> None:
    for entry in (SITE_PACKAGES, APP_DIR):
        resolved = str(entry)
        if resolved in sys.path:
            sys.path.remove(resolved)
        sys.path.insert(0, resolved)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"bootstrap: {name} is required")
    return value


def main() -> int:
    _configure_path()

    # `src.config` resolves relative paths against the working directory, and
    # `src.api.server` resolves the bundled plugin icons from its own location.
    # Chdir into the app dir so both agree.
    os.chdir(APP_DIR)

    host = os.environ.get("FINANCE_APP_HOST", "127.0.0.1")
    port = int(_require_env("FINANCE_APP_PORT"))

    import uvicorn

    # Plain asyncio/h11, not uvloop/httptools: the bundle deliberately omits
    # uvicorn[standard] (see macos/scripts/common.sh).
    uvicorn.run(
        "src.api.server:app",
        host=host,
        port=port,
        loop="asyncio",
        http="h11",
        log_level=os.environ.get("FINANCE_APP_LOG_LEVEL", "info"),
        access_log=False,  # access logs would carry query strings; see plan §9.
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
