"""Where payloads are exchanged.

## One file per device

Each device writes exactly one payload file, named from its own id:

    device-<device-id>.json

So no two devices ever write the same object and file-level conflicts cannot happen. That is the
single decision that makes the rest of this simple -- there is no locking, no compare-and-swap and no
fork detection, because there is nothing shared to fork.

Contrast the vault, whose one `latest.manifest.json` per vault is written by every device and where
the second writer silently orphans the first.

## Writes are atomic

A reader must never see half a payload. Local writes go to a temporary file in the same directory and
are then renamed, which is atomic within a filesystem.

What actually keeps a leftover temp file out of the reader's view is the **`device-` prefix**: the
reader matches `device-*.json`, and `tempfile` generates names like `tmpab12cd`, which cannot match
whatever suffix is used. `TEMP_SUFFIX` is belt-and-braces on top of that, not the protection --
setting it to `.json` changes nothing, which is worth stating because the obvious assumption is the
opposite. (Timeslice hit the real version of this bug by *constructing* its temp names from the final
name, so its temp files did carry the matching prefix.)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

PAYLOAD_PREFIX = "device-"
PAYLOAD_SUFFIX = ".json"
#: Deliberately not `.json.tmp`: a partial file must not match the reader's pattern.
TEMP_SUFFIX = ".partial"


@dataclass
class RemotePayload:
    device_id: str
    payload: dict
    #: When the *store* says the file changed, where the store can say. One server clock beats N
    #: device clocks, so this is preferred over the payload's self-reported `written_at` when judging
    #: freshness.
    modified_at: datetime | None = None


class SyncTransport(Protocol):
    def put(self, device_id: str, payload: dict) -> None: ...

    def fetch_others(self, device_id: str) -> list[RemotePayload]: ...

    def delete(self, device_id: str) -> None: ...

    def describe(self) -> str: ...


def payload_name(device_id: str) -> str:
    return f"{PAYLOAD_PREFIX}{device_id}{PAYLOAD_SUFFIX}"


def device_id_from_name(name: str) -> str | None:
    if not name.startswith(PAYLOAD_PREFIX) or not name.endswith(PAYLOAD_SUFFIX):
        return None
    return name[len(PAYLOAD_PREFIX) : -len(PAYLOAD_SUFFIX)] or None


class FolderTransport:
    """A directory both devices can see -- a shared volume, or iCloud/Dropbox on a Mac.

    Also what the tests run against: it exercises the real publish/fetch/merge path with no network
    and no credentials, which is the only way to test convergence honestly.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, device_id: str, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / payload_name(device_id)
        # Same directory, so the rename is atomic rather than a cross-filesystem copy.
        handle = tempfile.NamedTemporaryFile(
            mode="w", dir=self.root, suffix=TEMP_SUFFIX, delete=False, encoding="utf-8"
        )
        try:
            with handle:
                json.dump(payload, handle, default=str)
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, target)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    def fetch_others(self, device_id: str) -> list[RemotePayload]:
        if not self.root.exists():
            return []
        results: list[RemotePayload] = []
        for path in sorted(self.root.glob(f"{PAYLOAD_PREFIX}*{PAYLOAD_SUFFIX}")):
            peer = device_id_from_name(path.name)
            if peer is None or peer == device_id:
                continue
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                # One unreadable file must not stop syncing with every other device.
                logger.warning("sync: could not read peer payload %s", path.name)
                continue
            modified_at = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).replace(tzinfo=None)
            results.append(RemotePayload(peer, payload, modified_at))
        return results

    def delete(self, device_id: str) -> None:
        (self.root / payload_name(device_id)).unlink(missing_ok=True)

    def describe(self) -> str:
        return f"folder {self.root}"


class DriveTransport:
    """Google Drive, in a `devices` subfolder of the app's existing folder.

    **Not** the appData folder. That requires the `drive.appdata` scope, and this app is authorised
    for `drive.file` only -- using appData would send every existing user back through Google consent
    for no functional gain. `drive.file` grants access to files the app itself created, which is
    exactly what these are.
    """

    FOLDER_NAME = "devices"

    def __init__(self, access_token: str, *, folder_id: str | None = None):
        self.access_token = access_token
        self._folder_id = folder_id

    def folder_id(self) -> str:
        from src.vault.google_drive import ensure_child_folder, ensure_visible_app_folder

        if self._folder_id is None:
            app_folder = ensure_visible_app_folder(self.access_token)
            self._folder_id = ensure_child_folder(
                self.access_token, parent_id=app_folder["id"], name=self.FOLDER_NAME
            )["id"]
        return self._folder_id

    def put(self, device_id: str, payload: dict) -> None:
        from src.vault.google_drive import list_drive_files, upload_multipart_file

        name = payload_name(device_id)
        parent = self.folder_id()
        content = json.dumps(payload, default=str).encode()

        existing = _newest_by_name(list_drive_files(self.access_token, name=name, parent_id=parent))
        upload_multipart_file(
            self.access_token,
            name=name,
            content_bytes=content,
            mime_type="application/json",
            parent_id=parent,
            # Update in place when it exists. Drive permits duplicate names, and creating a new file
            # each publish is how one device ends up appearing a dozen times in the peer list.
            file_id=existing["id"] if existing else None,
        )

    def fetch_others(self, device_id: str) -> list[RemotePayload]:
        from src.vault.google_drive import download_file_bytes, list_drive_files
        from src.sync.coding import datetime_from_wire

        files = list_drive_files(self.access_token, parent_id=self.folder_id())
        by_device: dict[str, dict] = {}
        for entry in files:
            peer = device_id_from_name(entry.get("name") or "")
            if peer is None or peer == device_id:
                continue
            # Keep the newest when Drive has handed us duplicates of one name.
            current = by_device.get(peer)
            if current is None or (entry.get("modifiedTime") or "") > (
                current.get("modifiedTime") or ""
            ):
                by_device[peer] = entry

        results: list[RemotePayload] = []
        for peer, entry in sorted(by_device.items()):
            try:
                raw = download_file_bytes(self.access_token, entry["id"])
                payload = json.loads(raw)
            except Exception:
                logger.warning("sync: could not read peer payload for %s", peer)
                continue
            results.append(
                RemotePayload(peer, payload, datetime_from_wire(entry.get("modifiedTime")))
            )
        return results

    def delete(self, device_id: str) -> None:
        from src.vault.google_drive import list_drive_files

        name = payload_name(device_id)
        for entry in list_drive_files(self.access_token, name=name, parent_id=self.folder_id()):
            _trash_file(self.access_token, entry["id"])

    def describe(self) -> str:
        return "google drive"


def _newest_by_name(entries: list[dict]) -> dict | None:
    if not entries:
        return None
    return max(entries, key=lambda e: e.get("modifiedTime") or "")


def _trash_file(access_token: str, file_id: str) -> None:
    """Trash rather than hard-delete: a payload is reconstructible, but an accidental permanent
    delete of the wrong file is not."""
    import json as _json
    from urllib.request import Request

    from src.vault.google_drive import GOOGLE_DRIVE_FILES_URL, _json_request

    request = Request(
        f"{GOOGLE_DRIVE_FILES_URL}/{file_id}",
        data=_json.dumps({"trashed": True}).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    _json_request(request)
