"""This device's identity.

Stored in a `device-id` file beside the database, and **deliberately not synced**. Restoring another
device's identity would make two devices claim to be the same one, and each would then ignore the
other's payload -- they would look connected and silently never exchange anything.

It lives beside the database rather than in it for the same reason: a database copied to a second
machine (which is exactly how this project's macOS app started life) must not carry the first
machine's identity with it. Copy the database, get a new identity, sync normally.

The label is separate and is the user's to set. It exists to be different -- it is what tells two
Macs apart in the device list.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import uuid
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)

DEVICE_ID_FILENAME = "device-id"

PLATFORM_MACOS = "macos"
PLATFORM_IOS = "ios"
PLATFORM_CONTAINER = "container"
PLATFORM_UNKNOWN = "unknown"


def device_id_path() -> Path:
    return Path(settings.app.runtime_dir) / DEVICE_ID_FILENAME


def current_device_id() -> str:
    """This device's id, minted once and then stable.

    Written before it is returned, so a crash between minting and first use cannot produce two ids.
    """
    path = device_id_path()
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        logger.exception("sync: could not read the device id; minting a fresh one")

    minted = f"{detect_platform()}-{uuid.uuid4().hex[:8]}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(minted + "\n")
        # The id is not a secret, but it is a stable identifier for this machine, so it does not need
        # to be world-readable either.
        os.chmod(path, 0o600)
    except OSError:
        # Better to run with an ephemeral id than to refuse to start. It shows up as a new device in
        # the list, which is visible and fixable, rather than as a silent failure to sync.
        logger.exception("sync: could not persist the device id; using an ephemeral one")
    return minted


def detect_platform() -> str:
    """Which kind of device this is. Coarse on purpose -- it only groups the device list."""
    override = os.getenv("FINANCE_APP_PLATFORM")
    if override:
        return _slug(override)

    # The macOS bundle sets this; without it a Mac running the container flow would claim to be a
    # desktop app, which is the distinction the user actually cares about in the device list.
    if os.getenv("FINANCE_APP_LOCAL_TOKEN"):
        return PLATFORM_MACOS
    if _in_container():
        return PLATFORM_CONTAINER
    system = platform.system()
    if system == "Darwin":
        return PLATFORM_MACOS
    if system in ("Linux", "Windows"):
        return PLATFORM_CONTAINER if _in_container() else _slug(system)
    return PLATFORM_UNKNOWN


def _in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


def default_device_label() -> str:
    """A human name for this device, used until the user renames it."""
    host = (os.getenv("FINANCE_APP_DEVICE_LABEL") or platform.node() or "").strip()
    # A hostname that is just hex is a MAC-address-style name and is worse than useless as a label.
    if host and not re.fullmatch(r"[0-9a-fA-F:._-]{12,}", host):
        return host.split(".")[0]
    return {
        PLATFORM_MACOS: "This Mac",
        PLATFORM_IOS: "iPhone",
        PLATFORM_CONTAINER: "Container app",
    }.get(detect_platform(), "This device")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or PLATFORM_UNKNOWN


# --- the registry -----------------------------------------------------------------------------


def remember_device(
    db,
    device_id: str,
    *,
    label: str | None = None,
    platform_name: str | None = None,
    last_seen_at=None,
    watermark=None,
) -> None:
    """Upsert a device into the local registry.

    Non-destructive: a payload that omits a label must not erase the label we already have, and
    `last_seen_at` only ever moves forward, so payloads arriving out of order do not make a device
    look older than it is.
    """
    from src.models import SyncDevice

    row = db.get(SyncDevice, device_id)
    if row is None:
        row = SyncDevice(device_id=device_id)
        db.add(row)

    if label:
        row.label = label
    if platform_name:
        row.platform = platform_name
    if last_seen_at and (row.last_seen_at is None or last_seen_at > row.last_seen_at):
        row.last_seen_at = last_seen_at
    if watermark and (
        row.last_merged_watermark is None or watermark > row.last_merged_watermark
    ):
        row.last_merged_watermark = watermark


def known_devices(db) -> list:
    from src.models import SyncDevice

    return db.query(SyncDevice).order_by(SyncDevice.device_id).all()


def watermark_for(db, device_id: str):
    from src.models import SyncDevice

    row = db.get(SyncDevice, device_id)
    return row.last_merged_watermark if row else None
