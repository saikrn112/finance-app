"""Backup as a dated database snapshot, not a bundle.

## Why this replaced the bundle

The vault backup zipped everything under `settings.app.data_dir`, which on a real install meant
uploading ~475 MB to preserve a 9.3 MB database: 410 MB of throwaway replay-stack data, 141 MB of
import scratch, 53 MB of `finances.pre_*` copies left by past repairs, and -- because the sweep
included the old backups directory -- fossils of earlier backups too. Around 96% waste, growing.

It also carried a manifest lineage (`vault_id`, `backup_id`, `parent_backup_id`) whose only real
job was ordering backups, and which was broken for the case it existed to serve: two devices each
had their own `vault_id`, and the second to back up overwrote the shared "latest" manifest without
comparing parents, silently orphaning the first device's head.

So: one file per backup, named by time, in one shared folder. Ordering comes from the name. There
is nothing to parse and nothing to reconcile, and a snapshot can be restored by hand -- download
it and put it where the database goes.

## What a snapshot is, and is not

`VACUUM INTO`, not a file copy. A live SQLite database copied byte-wise can be caught mid-write
and yield a torn file that looks fine until it is needed; `VACUUM INTO` produces a consistent
standalone database and compacts it on the way out. (`src/vault/backup.py` reached for the
`sqlite3` backup API for the same reason.)

Only the primary database is read, which is what excludes the replay stack, the demo database,
the `.db` fossils beside the live one, and the import preview workspace -- by construction rather
than by an exclusion list that has to be maintained.

Statements are handled separately by `archive_statements`. They are content-addressed and
uploaded once each, because a bank statement never changes: re-sending 120 MB of unchanged PDFs
on every backup is exactly the waste this module exists to remove.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from src.config import settings

logger = logging.getLogger(__name__)

#: Sibling of the sync payload folder, under the same visible app folder.
SNAPSHOTS_FOLDER = "snapshots"
STATEMENTS_FOLDER = "statements"

DB_PREFIX = "finances-"
DB_SUFFIX = ".db"
CONFIG_PREFIX = "config-"
CONFIG_SUFFIX = ".yaml"

#: How many snapshots to keep. Deletions propagate through sync within a poll, so a single
#: overwritten file would give a one-round recovery window; a fortnight of dailies is the point.
SNAPSHOT_RETENTION = 14

_STAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)")


# --- naming -------------------------------------------------------------------------------------


def timestamp_slug(when: datetime | None = None) -> str:
    """A filename-safe UTC stamp. Colons are not safe in filenames on every platform."""
    moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z").replace(":", "-")


def snapshot_name(slug: str) -> str:
    return f"{DB_PREFIX}{slug}{DB_SUFFIX}"


def config_name(slug: str) -> str:
    return f"{CONFIG_PREFIX}{slug}{CONFIG_SUFFIX}"


def created_at_from_name(name: str) -> str | None:
    """Recover the ISO timestamp a snapshot was named with.

    The name is the only ordering key, deliberately -- no manifest to read. Returns None for a
    name that does not carry a stamp, so a stray file in the folder is ignored rather than
    sorting unpredictably among real snapshots.
    """
    match = _STAMP_PATTERN.search(name)
    if not match:
        return None
    stamp = match.group(1)
    return f"{stamp[:13]}:{stamp[14:16]}:{stamp[17:19]}Z"


# --- creating -----------------------------------------------------------------------------------


def create_snapshot(dest_dir: Path | None = None) -> Path:
    """A consistent, compacted copy of the primary database.

    Safe to run while the app is writing: VACUUM INTO reads through a transaction.
    """
    source = Path(settings.database.path)
    if not source.exists():
        raise HTTPException(status_code=400, detail=f"No database at {source}")

    target_dir = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="snapshot-"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / snapshot_name(timestamp_slug())
    if target.exists():
        target.unlink()

    # Read-only handle: this must never be the thing that writes to the live database.
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Snapshot failed: {exc}") from exc
    finally:
        conn.close()
    return target


def verify_snapshot(path: Path) -> dict[str, Any]:
    """Prove a snapshot is a usable database before it is trusted or promoted."""
    if not path.exists() or path.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="Snapshot is missing or empty")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise HTTPException(status_code=400, detail=f"Snapshot integrity_check: {integrity}")
        transactions = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    except sqlite3.Error as exc:
        raise HTTPException(status_code=400, detail=f"Snapshot is not a usable database: {exc}") from exc
    finally:
        conn.close()
    return {
        "integrity": integrity,
        "transactions": int(transactions),
        "size_bytes": path.stat().st_size,
    }


# --- Drive ---------------------------------------------------------------------------------------


def _folder_id(access_token: str, name: str) -> str:
    from src.vault.google_drive import ensure_child_folder, ensure_visible_app_folder

    app_folder = ensure_visible_app_folder(access_token)
    return ensure_child_folder(access_token, parent_id=app_folder["id"], name=name)["id"]


def list_snapshots(access_token: str) -> list[dict[str, Any]]:
    """Snapshots on Drive, newest first. Ordered by the name, which is the timestamp."""
    from src.vault.google_drive import list_drive_files

    parent = _folder_id(access_token, SNAPSHOTS_FOLDER)
    rows: list[dict[str, Any]] = []
    for entry in list_drive_files(access_token, parent_id=parent):
        name = entry.get("name") or ""
        if not (name.startswith(DB_PREFIX) and name.endswith(DB_SUFFIX)):
            continue
        created = created_at_from_name(name)
        if not created:
            continue
        rows.append(
            {
                "name": name,
                "file_id": entry["id"],
                "created_at": created,
                "size_bytes": int(entry.get("size") or 0),
            }
        )
    rows.sort(key=lambda row: row["name"], reverse=True)
    return rows


def publish_snapshot(
    access_token: str,
    *,
    retention: int = SNAPSHOT_RETENTION,
    on_progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    """Take a snapshot, upload it beside config.yaml, and prune old ones.

    `config.yaml` travels with it so a second device does not have to be handed Plaid and Google
    client credentials by hand. It is small; the database it accompanies already contains Plaid
    access tokens, so this does not change what a compromised Drive account would yield.
    """
    from src.vault.google_drive import upload_file_resumable, upload_multipart_file

    parent = _folder_id(access_token, SNAPSHOTS_FOLDER)
    workdir = Path(tempfile.mkdtemp(prefix="snapshot-publish-"))
    try:
        if on_progress:
            on_progress("snapshotting", 10)
        path = create_snapshot(workdir)
        stats = verify_snapshot(path)
        slug = _STAMP_PATTERN.search(path.name).group(1)

        if on_progress:
            on_progress("uploading", 40)
        uploaded = upload_file_resumable(
            access_token,
            name=path.name,
            file_path=path,
            mime_type="application/vnd.sqlite3",
            parent_id=parent,
        )

        config_file = None
        config_path = Path("config.yaml")
        if config_path.exists():
            config_file = upload_multipart_file(
                access_token,
                name=config_name(slug),
                content_bytes=config_path.read_bytes(),
                mime_type="application/x-yaml",
                parent_id=parent,
            )

        if on_progress:
            on_progress("pruning", 85)
        pruned = prune_snapshots(access_token, retention=retention)

        if on_progress:
            on_progress("done", 100)
        return {
            "name": path.name,
            "file_id": uploaded.get("id"),
            "config_file_id": (config_file or {}).get("id"),
            "created_at": created_at_from_name(path.name),
            "pruned": pruned,
            **stats,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def prune_snapshots(access_token: str, *, retention: int = SNAPSHOT_RETENTION) -> list[str]:
    """Keep the newest `retention` snapshots; trash the rest, with their config siblings.

    Trashed rather than hard-deleted: a snapshot is the thing you reach for when something has
    already gone wrong, and an accidental permanent delete of the wrong one is unrecoverable.
    """
    from src.sync.transport import _trash_file
    from src.vault.google_drive import list_drive_files

    if retention <= 0:
        return []
    snapshots = list_snapshots(access_token)
    doomed = snapshots[retention:]
    if not doomed:
        return []

    parent = _folder_id(access_token, SNAPSHOTS_FOLDER)
    configs = {
        entry.get("name"): entry["id"]
        for entry in list_drive_files(access_token, parent_id=parent)
        if (entry.get("name") or "").startswith(CONFIG_PREFIX)
    }
    removed: list[str] = []
    for row in doomed:
        _trash_file(access_token, row["file_id"])
        removed.append(row["name"])
        stamp = _STAMP_PATTERN.search(row["name"])
        if stamp:
            sibling = configs.get(config_name(stamp.group(1)))
            if sibling:
                _trash_file(access_token, sibling)
    logger.info("snapshot prune trashed %d old snapshot(s)", len(removed))
    return removed


def download_snapshot(access_token: str, file_id: str, dest: Path, *, expected_size: int | None = None) -> Path:
    from src.vault.google_drive import download_file_to_path

    dest.parent.mkdir(parents=True, exist_ok=True)
    download_file_to_path(access_token, file_id, dest, expected_size=expected_size)
    verify_snapshot(dest)
    return dest


def promote_snapshot(path: Path) -> dict[str, Any]:
    """Put a verified snapshot in place of the live database, keeping the displaced one.

    The displaced database is moved aside rather than deleted: restoring the wrong snapshot is a
    mistake someone will make, and it should be undoable without going back to Drive.
    """
    stats = verify_snapshot(path)
    target = Path(settings.database.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    displaced = None
    if target.exists():
        displaced = target.with_name(f"{target.stem}.replaced-{timestamp_slug()}{target.suffix}")
        shutil.move(str(target), str(displaced))
    try:
        shutil.move(str(path), str(target))
    except Exception as exc:
        if displaced and displaced.exists():
            shutil.move(str(displaced), str(target))
        raise HTTPException(status_code=500, detail=f"Restore promotion failed: {exc}") from exc

    # Sidecars belong to the database that was just replaced; leaving them lets SQLite replay
    # stale pages over the snapshot.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = target.with_name(target.name + suffix)
        sidecar.unlink(missing_ok=True)

    return {**stats, "displaced": str(displaced) if displaced else None}


# --- statements ----------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_statements(access_token: str, *, source_dir: Path | None = None) -> dict[str, Any]:
    """Upload original statements, content-addressed, skipping any already there.

    Named by content hash so the second run uploads nothing: statements are immutable, and the
    alternative -- re-sending them with every backup -- is most of what made the old bundle
    enormous. Extension is preserved so a human browsing Drive can still open them.
    """
    from src.vault.google_drive import list_drive_files, upload_file_resumable

    root = Path(source_dir) if source_dir else Path(settings.app.data_dir) / "raw"
    if not root.exists():
        return {"uploaded": 0, "skipped": 0, "bytes": 0}

    parent = _folder_id(access_token, STATEMENTS_FOLDER)
    existing = {entry.get("name") for entry in list_drive_files(access_token, parent_id=parent)}

    uploaded = skipped = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        name = f"{_sha256(path)}{path.suffix.lower()}"
        if name in existing:
            skipped += 1
            continue
        upload_file_resumable(
            access_token,
            name=name,
            file_path=path,
            mime_type="application/octet-stream",
            parent_id=parent,
        )
        existing.add(name)
        uploaded += 1
        total_bytes += path.stat().st_size
    logger.info("statement archive uploaded %d, skipped %d", uploaded, skipped)
    return {"uploaded": uploaded, "skipped": skipped, "bytes": total_bytes}
