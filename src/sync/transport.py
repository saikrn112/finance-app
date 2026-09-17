"""Where payloads are exchanged.

## One file per device

Each device writes exactly one payload file, named from its own id:

    device-<device-id>.json

So no two devices ever write the same object and file-level conflicts cannot happen. That is the
single decision that makes the rest of this simple -- there is no locking, no compare-and-swap and no
fork detection, because there is nothing shared to fork.

Contrast the vault, whose one `latest.manifest.json` per vault is written by every device and where
the second writer silently orphans the first.

## Google Drive only

There is deliberately one transport. A directory-based one existed briefly and was removed: a folder
only reaches processes that can see that filesystem, so it is single-machine sync wearing the label of
multi-device sync -- a phone or a second Mac could never join. Tests use a dict-backed fake instead,
which is a better test double anyway.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

logger = logging.getLogger(__name__)

PAYLOAD_PREFIX = "device-"
PAYLOAD_SUFFIX = ".json"


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

    def has_payload(self, device_id: str) -> bool:
        """Whether this device's own payload is still on the transport.

        Used to stop the publish-skip trusting a local fingerprint after the payload has been
        removed. Optional: the engine assumes "present" when a transport does not implement it.
        """
        ...

    def describe(self) -> str: ...


def payload_name(device_id: str) -> str:
    return f"{PAYLOAD_PREFIX}{device_id}{PAYLOAD_SUFFIX}"


def device_id_from_name(name: str) -> str | None:
    if not name.startswith(PAYLOAD_PREFIX) or not name.endswith(PAYLOAD_SUFFIX):
        return None
    return name[len(PAYLOAD_PREFIX) : -len(PAYLOAD_SUFFIX)] or None


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

    def has_payload(self, device_id: str) -> bool:
        from src.vault.google_drive import list_drive_files

        return bool(
            list_drive_files(
                self.access_token, name=payload_name(device_id), parent_id=self.folder_id()
            )
        )

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
