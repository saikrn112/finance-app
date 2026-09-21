"""Copying and tidying the Drive folder the app owns.

Two jobs, deliberately separate from `snapshot.py`, which is about taking backups:

`copy_folder` duplicates the whole vault server-side, as a safety net before anything destructive.
It uses Drive's `files.copy`, so nothing is downloaded or re-uploaded -- one API call per file and
no bandwidth. It does consume quota, though: copying a vault whose bulk is legacy bundles doubles
the thing the tidy is about to remove, so copy after converting what is worth keeping, not before.

`tidy` removes what is provably redundant, and nothing else:

* **Duplicate statements.** Drive allows several files to share a name in one folder, and the
  content-addressed archive relied on a name lookup that silently truncated at 100 results before
  pagination was fixed. The result was the same document uploaded up to three times -- 493 files for
  231 documents, ~141 MB redundant. One copy of each name is kept.
* **The legacy per-device vaults.** `<vault_id>/backups/<backup_id>/*.fvault` plus the
  `latest.manifest.json` beside them, and any loose `.fvault` at the root. Superseded by
  `snapshots/`; convert anything worth keeping first with `convert-legacy-backups`.

## Identifying legacy by evidence, not by exclusion

A folder is legacy when its name is a vault UUID *and* it actually contains a `.fvault` somewhere
beneath it. The obvious rule -- "any top-level folder that is not one of ours" -- is what a safety
copy of the vault trips over: `Finance Vault copy <date>` is not in `CURRENT_FOLDERS`, so a tidy run
would have trashed the backup taken to protect the tidy. Anything unrecognised is reported and left
alone instead.

Sizes are measured by walking the whole subtree. Counting one or two levels down reports these
vaults as empty, because the archives sit three levels below the root and intermediate folders have
no size of their own -- which is exactly how 28.5 GB of bundles got reported as "0.0 MB".

Everything is trashed rather than hard-deleted, and previewed before it is applied. Both matter: a
backup folder is what you reach for when something has already gone wrong, and a maintenance job
that guesses is worse than no maintenance job. Trash does not free quota until it is emptied.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"

#: Folders the current design owns.
CURRENT_FOLDERS = ("devices", "snapshots", "statements")

LEGACY_ARCHIVE_SUFFIX = ".fvault"
#: Legacy manifests appear as both `<prefix>-<vault_id>-latest.manifest.json` and
#: `<prefix>-<vault_id>-manifest.json`. Matching a bare `manifest.json` would also catch
#: `statements/manifest.json`, which the current design depends on, so the separator is required.
LEGACY_MANIFEST_MARKERS = ("-manifest.json", ".manifest.json")

#: Legacy vault folders are named with the `vault_id` UUID that minted them.
_VAULT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.IGNORECASE)


def _drive():
    from src.vault import google_drive

    return google_drive


def copy_file(access_token: str, file_id: str, *, name: str, parent_id: str) -> dict[str, Any]:
    """Server-side copy. No download, no re-upload."""
    from urllib.request import Request

    gd = _drive()
    request = Request(
        f"{gd.GOOGLE_DRIVE_FILES_URL}/{file_id}/copy",
        data=json.dumps({"name": name, "parents": [parent_id]}).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method="POST",
    )
    return gd._json_request(request)


def copy_folder(
    access_token: str,
    source_id: str,
    *,
    name: str,
    parent_id: str | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    """Recursively duplicate a folder. Returns counts and the new folder's id.

    Drive has no recursive copy, so this walks the tree and copies each file. Folders are created
    rather than copied, because copying a folder in Drive copies only the folder.
    """
    gd = _drive()
    root = gd.ensure_visible_app_folder(access_token) if parent_id is None else {"id": parent_id}
    destination = gd.ensure_child_folder(access_token, parent_id=root["id"], name=name)

    copied = folders = failed = 0
    total_bytes = 0

    def walk(src_id: str, dst_id: str) -> None:
        nonlocal copied, folders, failed, total_bytes
        for entry in gd.list_drive_files(access_token, parent_id=src_id):
            # The copy lives inside the folder being copied, so it must be skipped or the walk
            # descends into its own output and never terminates.
            if entry["id"] == destination["id"]:
                continue
            if entry.get("mimeType") == FOLDER_MIME:
                child = gd.ensure_child_folder(
                    access_token, parent_id=dst_id, name=entry["name"]
                )
                folders += 1
                walk(entry["id"], child["id"])
                continue
            try:
                copy_file(access_token, entry["id"], name=entry["name"], parent_id=dst_id)
                copied += 1
                total_bytes += int(entry.get("size") or 0)
                if on_progress and copied % 25 == 0:
                    on_progress(f"copied {copied} file(s)", copied)
            except Exception:
                # One unreadable file must not abandon the copy; the count is reported instead.
                logger.warning("drive copy: could not copy %s", entry.get("name"), exc_info=True)
                failed += 1

    walk(source_id, destination["id"])
    return {
        "folder": name,
        "folder_id": destination["id"],
        "files_copied": copied,
        "folders_created": folders,
        "failed": failed,
        "bytes": total_bytes,
    }


# --- tidying -------------------------------------------------------------------------------------


def walk_tree(access_token: str, folder_id: str) -> tuple[list[dict], list[dict]]:
    """Every file and folder beneath `folder_id`, to any depth. Returns (files, folders).

    Iterative rather than recursive so a deep or cyclic-looking tree cannot blow the stack, and so
    the byte total covers the whole subtree instead of the first level or two. Folders carry a
    `depth`, which is what lets a caller delete bottom-up.
    """
    gd = _drive()
    files: list[dict] = []
    folders: list[dict] = []
    pending: list[tuple[str, int]] = [(folder_id, 0)]
    seen: set[str] = set()
    while pending:
        current, depth = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for entry in gd.list_drive_files(access_token, parent_id=current):
            if entry.get("mimeType") == FOLDER_MIME:
                folders.append({**entry, "depth": depth + 1})
                pending.append((entry["id"], depth + 1))
            else:
                files.append(entry)
    return files, folders


def _survey(access_token: str, *, root_id: str | None = None) -> dict[str, Any]:
    gd = _drive()
    root = {"id": root_id} if root_id else gd.ensure_visible_app_folder(access_token)
    top = gd.list_drive_files(access_token, parent_id=root["id"])

    duplicate_statements: list[dict] = []
    statements_folder = next(
        (e for e in top if e.get("mimeType") == FOLDER_MIME and e["name"] == "statements"), None
    )
    if statements_folder:
        by_name: dict[str, list[dict]] = defaultdict(list)
        for entry in gd.list_drive_files(access_token, parent_id=statements_folder["id"]):
            if entry["name"] == "manifest.json":
                continue  # the current manifest, not a duplicated document
            by_name[entry["name"]].append(entry)
        for name, entries in by_name.items():
            if len(entries) <= 1:
                continue
            # Keep the oldest: it is the one the earliest backup referred to.
            entries.sort(key=lambda e: e.get("createdTime") or "")
            duplicate_statements.extend(entries[1:])

    legacy_folders: list[dict] = []
    unrecognised: list[dict] = []
    legacy_bytes = 0
    legacy_file_count = 0
    for entry in top:
        if entry.get("mimeType") != FOLDER_MIME or entry["name"] in CURRENT_FOLDERS:
            continue
        if not _VAULT_ID_PATTERN.match(entry["name"]):
            unrecognised.append(entry)
            continue
        files, _ = walk_tree(access_token, entry["id"])
        # A UUID name alone is not proof. The archives are -- or the absence of any file at all,
        # which is the husk left when Drive refuses to trash a folder whose contents have already
        # gone. Without the second case those husks linger as "unrecognised" forever.
        holds_archives = any((f.get("name") or "").endswith(LEGACY_ARCHIVE_SUFFIX) for f in files)
        if files and not holds_archives:
            unrecognised.append(entry)
            continue
        subtree_bytes = sum(int(f.get("size") or 0) for f in files)
        legacy_bytes += subtree_bytes
        legacy_file_count += len(files)
        legacy_folders.append({**entry, "files": len(files), "bytes": subtree_bytes})

    legacy_loose = [
        e for e in top
        if e.get("mimeType") != FOLDER_MIME
        and (
            e["name"].endswith(LEGACY_ARCHIVE_SUFFIX)
            or e["name"].endswith(LEGACY_MANIFEST_MARKERS)
        )
    ]
    legacy_bytes += sum(int(e.get("size") or 0) for e in legacy_loose)
    legacy_file_count += len(legacy_loose)

    return {
        "root_id": root["id"],
        "duplicate_statements": duplicate_statements,
        "legacy_folders": legacy_folders,
        "legacy_loose": legacy_loose,
        "unrecognised": unrecognised,
        "legacy_file_count": legacy_file_count,
        "legacy_bytes": legacy_bytes,
    }


def preview_tidy(access_token: str, *, root_id: str | None = None) -> dict[str, Any]:
    """What `tidy` would remove. Reads only."""
    survey = _survey(access_token, root_id=root_id)
    dupe_bytes = sum(int(e.get("size") or 0) for e in survey["duplicate_statements"])
    return {
        "duplicate_statement_files": len(survey["duplicate_statements"]),
        "duplicate_statement_mb": round(dupe_bytes / 1048576, 1),
        "legacy_vault_folders": [
            {"name": e["name"][:8] + "...", "files": e["files"], "gb": round(e["bytes"] / 1073741824, 2)}
            for e in survey["legacy_folders"]
        ],
        "legacy_files": survey["legacy_file_count"],
        "legacy_gb": round(survey["legacy_bytes"] / 1073741824, 2),
        "legacy_loose_files": [e["name"] for e in survey["legacy_loose"]],
        "left_alone": [e["name"] for e in survey["unrecognised"]],
        "total_gb": round((dupe_bytes + survey["legacy_bytes"]) / 1073741824, 2),
    }


def _trash_subtree(access_token: str, folder_id: str) -> tuple[int, int]:
    """Trash everything under a folder, then the folder. Returns (files, folders) trashed.

    Bottom-up and file by file, because the app holds the `drive.file` scope: Drive rejects
    trashing a folder while it still has children the app cannot prove it may touch
    (`appNotAuthorizedToChild`), so trashing the parent alone fails outright.

    A folder that still refuses is logged and skipped. An empty folder left behind costs nothing;
    abandoning the remaining 28 GB because one directory would not go is the worse outcome.
    """
    from src.sync.transport import _trash_file

    files, folders = walk_tree(access_token, folder_id)
    trashed_files = 0
    for entry in files:
        try:
            _trash_file(access_token, entry["id"])
            trashed_files += 1
        except Exception:
            logger.warning("drive tidy: could not trash a file under %s", folder_id, exc_info=True)

    trashed_folders = 0
    # Deepest first: a parent cannot go before its children.
    for entry in sorted(folders, key=lambda row: row.get("depth", 0), reverse=True) + [
        {"id": folder_id, "depth": 0}
    ]:
        try:
            _trash_file(access_token, entry["id"])
            trashed_folders += 1
        except Exception:
            logger.warning("drive tidy: could not trash folder %s", entry["id"], exc_info=True)
    return trashed_files, trashed_folders


def tidy(
    access_token: str, *, drop_legacy: bool = True, root_id: str | None = None
) -> dict[str, Any]:
    """Trash the redundant copies and, unless asked otherwise, the superseded vault folders."""
    from src.sync.transport import _trash_file

    survey = _survey(access_token, root_id=root_id)
    trashed_statements = 0
    for entry in survey["duplicate_statements"]:
        try:
            _trash_file(access_token, entry["id"])
            trashed_statements += 1
        except Exception:
            # One refusal must not abandon the run; the counts report what actually happened.
            logger.warning("drive tidy: could not trash a duplicate statement", exc_info=True)

    trashed_legacy = 0
    trashed_folders = 0
    reclaimed = 0
    if drop_legacy:
        for folder in survey["legacy_folders"]:
            files, folders = _trash_subtree(access_token, folder["id"])
            trashed_legacy += files
            trashed_folders += folders
            reclaimed += folder["bytes"]
        for entry in survey["legacy_loose"]:
            _trash_file(access_token, entry["id"])
            trashed_legacy += 1
            reclaimed += int(entry.get("size") or 0)

    logger.info(
        "drive tidy trashed %d duplicate statement(s) and %d legacy file(s)",
        trashed_statements, trashed_legacy,
    )
    return {
        "duplicate_statements_trashed": trashed_statements,
        "legacy_files_trashed": trashed_legacy,
        "legacy_folders_trashed": trashed_folders,
        "reclaimed_gb": round(reclaimed / 1073741824, 2),
        "left_alone": [e["name"] for e in survey["unrecognised"]],
    }
