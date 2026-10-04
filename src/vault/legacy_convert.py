"""One-shot: turn the newest legacy `.fvault` bundles into snapshots, then let the old path go.

66 bundles were written before `snapshot.py` existed, at 475-513 MB each, to preserve a 9.8 MB
database -- the rest was the replay stack, import scratch and `.db` repair fossils that the sweep
picked up along with it. They are the only backups predating the snapshot design, so the newest few
are worth keeping; keeping them *as bundles* is not, because the reader that understands them is
about to be deleted.

Conversion reads one member out of the zip -- the primary database -- verifies it is a usable
SQLite file, and uploads it under the snapshot naming scheme with the bundle's own creation time, so
it sorts into `snapshots/` in the right place. `config.yaml` comes across too when the bundle
carried one, because that is what the snapshot format pairs with a database.

Nothing here is reusable machinery. It exists to be run once and removed with the rest of the
legacy path.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from src.config import settings
from src.vault import snapshot as snap

logger = logging.getLogger(__name__)

LEGACY_ARCHIVE_SUFFIX = ".fvault"
LEGACY_MANIFEST_NAME = "backup.manifest.json"

#: Inside the bundle, app data sits under `state/app_data/` at paths relative to the data directory.
BUNDLE_STATE = "state"
BUNDLE_APP_DATA = f"{BUNDLE_STATE}/app_data"


def database_members(sizes: dict[str, int]) -> list[str]:
    """Candidate zip members holding the primary database, best first.

    A single best guess is not enough. Older installs shipped an empty `finance.db` beside the real
    `finances.db`, and picking alphabetically chose the empty one -- which failed as "no such table:
    transactions" and made two months of history look unconvertible. So: return an ordered list and
    let the caller accept the first that actually verifies.

    Order is the configured path, then largest first. Size is the honest signal for which of several
    `.db` files is the real one.
    """
    ordered: list[str] = []
    try:
        relative = Path(settings.database.path).resolve().relative_to(
            Path(settings.app.data_dir).resolve()
        ).as_posix()
        preferred = f"{BUNDLE_APP_DATA}/{relative}"
        if preferred in sizes:
            ordered.append(preferred)
    except ValueError:
        pass

    candidates = [
        name for name in sizes
        if name.startswith(f"{BUNDLE_APP_DATA}/")
        and name.endswith(".db")
        and "/replay" not in name
        and "/demo" not in name
        and ".pre_" not in name
        and ".replaced-" not in name
        and name not in ordered
    ]
    runtime = [name for name in candidates if "/runtime/" in name]
    ordered.extend(sorted(runtime or candidates, key=lambda name: sizes[name], reverse=True))
    return ordered


def repair_indexes(path: Path) -> bool:
    """`REINDEX` a database whose integrity failure is only an index inconsistency.

    One real bundle failed with "row N missing from index ix_category": the table data is intact and
    an index is stale, which REINDEX rebuilds. Discarding a month of history over a rebuildable
    index would be throwing away recoverable data.
    """
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("REINDEX")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    try:
        snap.verify_snapshot(path)
    except HTTPException:
        return False
    return True


def extract_from_bundle(archive_path: Path, dest_dir: Path) -> dict[str, Any]:
    """Pull the database (and config, if present) out of one bundle. No full extraction.

    Extracting the whole archive would write ~700 MB of replay data and import scratch to disk to
    reach a 9.8 MB file, which is the waste this conversion exists to undo.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    db_path = dest_dir / "finances.db"
    with zipfile.ZipFile(archive_path) as bundle:
        sizes = {info.filename: info.file_size for info in bundle.infolist()}

        created_at = None
        if LEGACY_MANIFEST_NAME in sizes:
            try:
                created_at = json.loads(bundle.read(LEGACY_MANIFEST_NAME)).get("created_at")
            except (ValueError, KeyError):
                created_at = None

        candidates = database_members(sizes)
        if not candidates:
            raise HTTPException(
                status_code=400,
                detail=f"No primary database inside {archive_path.name}",
            )

        config_bytes = None
        config_member = f"{BUNDLE_STATE}/config.yaml"
        if config_member in sizes:
            config_bytes = bundle.read(config_member)

        stats: dict[str, Any] | None = None
        member = None
        repaired = False
        last_error: HTTPException | None = None
        for candidate in candidates:
            with bundle.open(candidate) as src, db_path.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            try:
                stats = snap.verify_snapshot(db_path)
            except HTTPException as exc:
                last_error = exc
                if repair_indexes(db_path):
                    stats = snap.verify_snapshot(db_path)
                    repaired = True
                else:
                    continue
            member = candidate
            break

    if stats is None or member is None:
        raise last_error or HTTPException(
            status_code=400, detail=f"No usable database inside {archive_path.name}"
        )

    return {
        "database": db_path,
        "config_bytes": config_bytes,
        "created_at": created_at,
        "member": member,
        "repaired": repaired,
        **stats,
    }


# --- Drive side ----------------------------------------------------------------------------------


def _slug_from_created_at(created_at: str | None) -> str:
    """The bundle's own creation time, so converted snapshots sort by when they were taken.

    Manifest timestamps carry fractional seconds (`...T01:22:43.752Z`), which the snapshot stamp
    pattern does not accept. Dropping them is what makes the name match; without it every converted
    bundle silently fell back to "now" and a months-old backup sorted as today's.
    """
    if not created_at:
        return snap.timestamp_slug()
    stamp = re.sub(r"\.\d+(?=Z?$)", "", created_at.strip()).replace(":", "-")
    if not stamp.endswith("Z"):
        stamp += "Z"
    match = snap._STAMP_PATTERN.search(stamp)
    return match.group(1) if match else snap.timestamp_slug()


#: The old writer put the backup's own time in the filename:
#: `<prefix>-<vault_id>-<backup_id>-2026-05-07T00-46-33Z.fvault`.
_NAME_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})Z?" + re.escape(LEGACY_ARCHIVE_SUFFIX))


def stamp_from_archive_name(name: str) -> str | None:
    match = _NAME_STAMP.search(name)
    return f"{match.group(1)}Z" if match else None


def find_legacy_archives(access_token: str, *, root_id: str | None = None) -> list[dict[str, Any]]:
    """Every distinct `.fvault` under the vault, newest backup first.

    Ordered by the timestamp in the *filename*, not Drive's `createdTime`: a server-side copy of the
    vault carries today's creation time on every file, so sorting by it ranks copies of April
    backups above the genuinely newest ones. Copies are also collapsed here -- same backup
    timestamp, same bytes, and converting both would spend two of three slots on one backup.
    """
    from src.vault.drive_maintenance import walk_tree
    from src.vault.google_drive import ensure_visible_app_folder

    root = {"id": root_id} if root_id else ensure_visible_app_folder(access_token)
    files, _ = walk_tree(access_token, root["id"])

    best: dict[str, dict[str, Any]] = {}
    unstamped: list[dict[str, Any]] = []
    for entry in files:
        name = entry.get("name") or ""
        if not name.endswith(LEGACY_ARCHIVE_SUFFIX):
            continue
        row = {
            "id": entry["id"],
            "name": name,
            "size_bytes": int(entry.get("size") or 0),
            "created_time": entry.get("createdTime") or "",
            "backup_at": stamp_from_archive_name(name),
        }
        if row["backup_at"] is None:
            unstamped.append(row)
            continue
        held = best.get(row["backup_at"])
        # Prefer the largest (a truncated upload is not the one to convert), then the earliest
        # created, which is the original rather than a copy of it.
        if held is None or (row["size_bytes"], held["created_time"]) > (
            held["size_bytes"], row["created_time"]
        ):
            best[row["backup_at"]] = row

    archives = sorted(best.values(), key=lambda row: row["backup_at"], reverse=True)
    archives.extend(sorted(unstamped, key=lambda row: row["created_time"], reverse=True))
    return archives


def select_archives(
    archives: list[dict[str, Any]], *, count: int = 3, monthly: bool = False
) -> list[dict[str, Any]]:
    """Which bundles are worth the download.

    `monthly` takes the newest bundle in each calendar month instead of the newest `count` overall,
    and it is usually what you want. The live vault made the reason concrete: the newest three
    bundles were all from the same three days that `snapshots/` already covered, so converting them
    would have preserved nothing, while one per month covered five months for the same ~10 MB each.
    """
    if not monthly:
        return archives[:count]
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for archive in archives:  # newest first, so the first of each month is that month's newest
        month = (archive.get("backup_at") or "")[:7]
        if month in seen:
            continue
        seen.add(month)
        picked.append(archive)
    return picked


def convert_newest(
    access_token: str,
    *,
    count: int = 3,
    monthly: bool = False,
    root_id: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Convert legacy bundles into snapshots on Drive.

    Existing snapshot names are skipped rather than re-uploaded, so this is safe to re-run. It
    deliberately does not prune: these timestamps are months old, and pruning here would let the
    daily window discard the history the moment it arrived. `snapshots_to_keep` is what protects it
    afterwards.
    """
    from src.vault.google_drive import (
        download_file_to_path,
        list_drive_files,
        upload_file_resumable,
        upload_multipart_file,
    )

    archives = select_archives(
        find_legacy_archives(access_token, root_id=root_id), count=count, monthly=monthly
    )
    if not archives:
        return {"converted": [], "skipped": [], "failed": [], "archives_seen": 0}

    parent = snap._folder_id(access_token, snap.SNAPSHOTS_FOLDER)
    existing = {entry.get("name") for entry in list_drive_files(access_token, parent_id=parent)}

    converted: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for archive in archives:
        workdir = Path(tempfile.mkdtemp(prefix="legacy-convert-"))
        try:
            if on_progress:
                on_progress(f"downloading {archive['name'][:40]} ({archive['size_bytes'] >> 20} MB)")
            local = workdir / "bundle.fvault"
            download_file_to_path(
                access_token, archive["id"], local, expected_size=archive["size_bytes"] or None
            )

            extracted = extract_from_bundle(local, workdir / "out")
            # The manifest is authoritative; the filename stamp is the fallback. Drive's
            # `createdTime` is not usable here -- on a copied vault it is the time of the copy.
            slug = _slug_from_created_at(extracted["created_at"] or archive.get("backup_at"))
            name = snap.snapshot_name(slug)
            if name in existing:
                skipped.append(name)
                continue

            if on_progress:
                on_progress(f"uploading {name} ({extracted['transactions']} transactions)")
            uploaded = upload_file_resumable(
                access_token,
                name=name,
                file_path=extracted["database"],
                mime_type="application/vnd.sqlite3",
                parent_id=parent,
            )
            if extracted["config_bytes"]:
                upload_multipart_file(
                    access_token,
                    name=snap.config_name(slug),
                    content_bytes=extracted["config_bytes"],
                    mime_type="application/x-yaml",
                    parent_id=parent,
                )
            existing.add(name)
            converted.append(
                {
                    "name": name,
                    "file_id": uploaded.get("id"),
                    "created_at": snap.created_at_from_name(name),
                    "transactions": extracted["transactions"],
                    "size_bytes": extracted["size_bytes"],
                    "from_bytes": archive["size_bytes"],
                    "repaired": extracted["repaired"],
                }
            )
        except Exception as exc:
            # One bad bundle must not stop the rest; the point is to rescue what is readable.
            logger.warning("legacy convert failed for %s", archive["name"], exc_info=True)
            failed.append({"name": archive["name"], "error": str(exc)})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return {
        "converted": converted,
        "skipped": skipped,
        "failed": failed,
        "archives_seen": len(archives),
    }
