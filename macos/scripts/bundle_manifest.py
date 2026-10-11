#!/usr/bin/env python3
"""Audit the assembled artifact and write content hashes before signing."""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

app, repo = map(Path, sys.argv[1:3])
dependency_fingerprint, configuration = sys.argv[3:5]
resources = app / "Contents/Resources"
revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
built = datetime.now(timezone.utc).isoformat()
# Finalize metadata before hashing so every recorded digest describes the signed artifact.
(resources / "backend/MANIFEST.txt").write_text("built: " + built + "\ncommit: " + revision +
                                             "\ndependencies: " + dependency_fingerprint + "\n")
files = {}
for path in sorted(app.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    relative = str(path.relative_to(app))
    if path.name in {"config.yaml", "finances.db", "google-token.json", ".env"} or path.suffix in {".db", ".sqlite", ".pyc"}:
        raise SystemExit("Refusing private/generated artifact: " + relative)
    data = path.read_bytes()
    # Dependencies may contain vendor build paths. Our executable and application resources may not.
    own_input = relative.startswith("Contents/MacOS/") or "/backend/app/" in relative or "/web/" in relative
    if own_input and b"/Users/" in data:
        raise SystemExit("Builder/user path in distributable: " + relative)
    files[relative] = hashlib.sha256(data).hexdigest()
manifest = {"revision": revision, "built": built, "configuration": configuration,
            "dependencies_sha256": dependency_fingerprint, "files": files}
(resources / "BUILD-MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
