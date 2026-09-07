"""Import every bundled dependency that ships compiled code, plus the app itself.

Run with the *signed* bundled interpreter. Signing and the hardened runtime only
fail at `dlopen` time, so a bundle that verifies with `codesign --verify` can
still be unable to load a single extension module. This is the check that
actually settles it.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "site-packages"))
sys.path.insert(0, str(HERE / "app"))

# Modules with native extensions, plus the pure-Python packages that matter.
MODULES = [
    "_sqlite3",
    "_ssl",
    "_hashlib",
    "zlib",
    "lzma",
    "bz2",
    "readline",
    "pydantic_core",
    "yaml",
    "sqlalchemy",
    "sqlalchemy.cyextension.collections",
    "PIL.Image",
    "PIL._imaging",
    "cffi",
    "cryptography.hazmat.bindings._rust",
    "pypdfium2",
    "pdfplumber",
    "fastapi",
    "uvicorn",
    "h11",
    "plaid",
    "click",
]

failed = []
for name in MODULES:
    try:
        __import__(name)
    except Exception as exc:  # noqa: BLE001 - report, don't classify
        failed.append((name, f"{type(exc).__name__}: {exc}"))

# The app's own tree, which pulls in nearly everything transitively.
for name in ["src.config", "src.models", "src.api.server", "src.main"]:
    try:
        __import__(name)
    except Exception as exc:  # noqa: BLE001
        failed.append((name, f"{type(exc).__name__}: {exc}"))

# The categorizer must work without google-generativeai, which the bundle omits.
try:
    import google.generativeai  # noqa: F401

    print("NOTE: google-generativeai IS present; the bundle was meant to omit it")
except ImportError:
    print("ok: google-generativeai absent as intended (AI categorisation unavailable)")

for name, err in failed:
    print(f"FAIL {name}: {err}")
print(f"{len(MODULES) + 4 - len(failed)}/{len(MODULES) + 4} imports ok")
sys.exit(1 if failed else 0)
