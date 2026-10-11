"""Choosing a transport and running one sync round.

The layer between "sync exists" and "sync runs": resolves which transport to use from configuration,
and is the single entry point for the CLI, the scheduler and any future UI button. One place, so those
three cannot drift in how they decide whether sync is enabled.

## Off unless asked

`FINANCE_APP_SYNC` gates everything. Absent or `0` means no transport is available and nothing runs,
which is the state every existing installation is in after this ships. Sync writes real financial
history to cloud storage and to peer devices; it should start only when someone has decided it should.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.sync.engine import SyncResult, run_sync
from src.sync.transport import DriveTransport, SyncTransport

logger = logging.getLogger(__name__)

ENABLE_VAR = "FINANCE_APP_SYNC"

MODE_OFF = "off"
MODE_DRIVE = "drive"


@dataclass
class TransportChoice:
    mode: str
    transport: SyncTransport | None
    reason: str


def sync_enabled() -> bool:
    return (os.getenv(ENABLE_VAR) or "").strip().lower() in ("1", "true", "yes", "on")


def choose_transport(db: Session) -> TransportChoice:
    """Which transport to use, and why -- the reason is meant to be shown to the user.

    Google Drive, or nothing. It reuses the vault's existing connection and is already authorised for
    `drive.file`, so no new consent is needed.

    There is deliberately no directory-based alternative. One existed briefly and was removed: a
    folder only reaches processes that can see that filesystem, which makes it single-machine sync
    wearing the label of multi-device sync.
    """
    if not sync_enabled():
        return TransportChoice(MODE_OFF, None, f"{ENABLE_VAR} is not set")

    from src.api.routes.settings import _google_access

    try:
        _log, access_token, _extra = _google_access(db)
    except Exception as exc:
        # Includes the ordinary "Google Drive is not connected" case, which is not an error worth a
        # traceback -- it is a thing for the user to do.
        detail = getattr(exc, "detail", None) or str(exc)
        return TransportChoice(MODE_OFF, None, f"no transport available: {detail}")

    return TransportChoice(MODE_DRIVE, DriveTransport(access_token), "google drive")


def sync_once(
    db: Session, *, device_label: str | None = None, force_publish: bool = False
) -> dict:
    """One round, if sync is enabled. Returns a summary safe to log or display.

    Never raises for "sync is not configured": callers include a background scheduler, and a missing
    setting is not a failure.

    `force_publish` republishes even when the content fingerprint says nothing changed. Needed
    after a restore -- so the restored state wins on peers deliberately rather than being merged
    back over -- and as the recovery path when a payload has gone missing from the transport.
    """
    choice = choose_transport(db)
    if choice.transport is None:
        return {"ran": False, "mode": choice.mode, "reason": choice.reason}

    from src.services import device_sync_health
    try:
        result: SyncResult = run_sync(
            db, choice.transport, device_label=device_label, force_publish=force_publish
        )
    except Exception:
        db.rollback()
        device_sync_health.record(db, successful=False)
        raise
    summary = {"ran": True, "mode": choice.mode, "transport": choice.transport.describe()}
    summary.update(result.as_dict())
    problem = convergence_problem(summary)
    device_sync_health.record(db, successful=problem is None, problem=problem)
    return summary


def convergence_problem(result: dict) -> str | None:
    """A completed request is not proof that peer records converged."""
    if not result.get("ran") or result.get("error") or result.get("peers_failed"):
        return "Device sync did not complete. Check Google Drive and try again."
    reports = list((result.get("merges") or {}).values())
    if any(report.get("awaiting_consent") for report in reports):
        return "Review the first device merge in Settings before these devices can converge."
    if any(report.get("fact_conflicts") or report.get("rename_conflicts") for report in reports):
        return "Some device records conflict and need review. No conflicting financial facts were overwritten."
    if any(report.get("unresolved") for report in reports):
        return "Some device links could not be resolved. Sync again after reviewing the peer devices."
    return None
