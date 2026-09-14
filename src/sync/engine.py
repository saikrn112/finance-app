"""One sync round: publish, fetch, merge.

    for each peer:
        merge(peer.payload)      # newest first, so the freshest data lands before stale echoes
    publish(own payload)         # after merging, so peers see the merged result in one hop

## Why publish last

Publishing after merging means this device's file already contains everything it just learned, so a
third device gets the whole picture from one file instead of having to reach every other device
directly. With three devices and a phone that is rarely awake, that is the difference between
converging and not.

## The Plaid economy

Plaid bills per call, and a transaction pulled on the Mac is the same transaction on the phone. So
each payload reports **when this device last pulled from Plaid** -- a timestamp only. The cursor and
the access token live in `sync_log`, which never leaves the device.

`plaid_pull_is_needed` then answers "has anybody pulled recently?" instead of "have *I* pulled
recently?", which is what turns N devices into roughly one device's worth of Plaid spend.

Two devices can still decide to pull at the same moment. That is a wasted call, not a data problem:
`(source, source_id)` is a unique index, so the same transaction cannot be stored twice no matter how
many devices fetch it. A lease narrows the window; it does not need to close it, and paying for
correctness with a distributed lock here would be a bad trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from src.sync import coding, device
from src.sync.merge import MergeReport, merge_payload
from src.sync.payload import build_payload
from src.sync.transport import RemotePayload, SyncTransport

logger = logging.getLogger(__name__)

#: How long a peer's report of "I pulled from Plaid" is trusted. Comfortably longer than a sync poll
#: so a device that is merely slow to publish does not trigger a duplicate pull.
PLAID_REPORT_TRUST_WINDOW = timedelta(hours=36)


@dataclass
class SyncResult:
    device_id: str
    peers_seen: list[str] = field(default_factory=list)
    peers_failed: list[str] = field(default_factory=list)
    merges: dict[str, dict] = field(default_factory=dict)
    published: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "peers_seen": self.peers_seen,
            "peers_failed": self.peers_failed,
            "merges": self.merges,
            "published": self.published,
            "error": self.error,
        }


def run_sync(
    db: Session,
    transport: SyncTransport,
    *,
    device_id: str | None = None,
    device_label: str | None = None,
    full: bool = False,
) -> SyncResult:
    """One full round against `transport`.

    `full=True` republishes everything instead of only what changed since the last publish. Needed
    after a schema or derivation change, when a peer's watermark would otherwise skip rows it has
    never actually seen.
    """
    device_id = device_id or device.current_device_id()
    device_label = device_label or device.default_device_label()
    platform_name = device.detect_platform()
    result = SyncResult(device_id=device_id)

    incoming = _fetch(transport, device_id, result)
    for remote in incoming:
        try:
            report = merge_payload(db, remote.payload)
            result.merges[remote.device_id] = report.as_dict()
            device.remember_device(
                db,
                remote.device_id,
                label=remote.payload.get("device_label"),
                platform_name=remote.payload.get("platform"),
                # The store's own timestamp where it has one: a peer's self-reported clock can be
                # wrong, and this is used to judge freshness.
                last_seen_at=remote.modified_at
                or coding.datetime_from_wire(remote.payload.get("written_at")),
                watermark=report.watermark,
            )
            # Remember what this peer says about its Plaid usage, so the decision to spend a Plaid
            # call later needs no network round trip of its own.
            note_peer_plaid_report(db, remote.device_id, remote.payload)
            db.commit()
        except Exception as exc:
            # One bad payload must not stop syncing with every other device.
            logger.exception("sync: merging the payload from %s failed", remote.device_id)
            result.peers_failed.append(remote.device_id)
            result.merges[remote.device_id] = {"error": str(exc)}
            db.rollback()

    try:
        # Full state, always. See _own_watermark for why the obvious optimisation is wrong.
        since = None
        payload = build_payload(
            db,
            device_id=device_id,
            device_label=device_label,
            platform=platform_name,
            since=since,
        )
        payload["plaid"] = _plaid_report(db)
        transport.put(device_id, payload)
        _record_own_publish(db, device_id, device_label, platform_name, payload)
        db.commit()
        result.published = True
    except Exception as exc:
        logger.exception("sync: publishing this device's payload failed")
        result.error = str(exc)
        db.rollback()

    return result


def _fetch(transport: SyncTransport, device_id: str, result: SyncResult) -> list[RemotePayload]:
    try:
        remotes = transport.fetch_others(device_id)
    except Exception as exc:
        logger.exception("sync: could not list peers on %s", transport.describe())
        result.error = str(exc)
        return []

    # Newest first, so the freshest values land before an older echo of the same row. The merge is
    # order-independent by design, so this is an efficiency, not a correctness requirement.
    remotes.sort(
        key=lambda r: r.modified_at or coding.datetime_from_wire(r.payload.get("written_at")) or datetime.min,
        reverse=True,
    )
    result.peers_seen = [r.device_id for r in remotes]
    return remotes


# --- watermarks -------------------------------------------------------------------------------

_OWN_WATERMARK_KEY = "sync_own_published_watermark"


def _own_watermark(db: Session, device_id: str) -> datetime | None:
    """The newest row this device has published. **Not used to filter publishing** -- see below.

    Publishing only rows newer than this is the obvious way to stop payloads growing without bound,
    and it is wrong. A row merged from a peer carries *that peer's* `updated_at`, and peer clocks are
    independent, so a freshly-learned row routinely has a timestamp older than this device's own
    watermark. It is then excluded from the next publish and never relayed onward -- so a third
    device, or one that only ever reads this device's file, silently never receives it.

    The randomised soak test caught exactly that: devices ended up with different *sets* of
    transactions, each missing rows the other had learned.

    Doing this correctly needs a local monotonic marker ("this row changed on this device at local
    sequence N") that is bumped by merges as well as by user edits, which `updated_at` cannot be
    because it deliberately carries the *originating* device's time. That is a schema addition and a
    write hook; until then payloads are full state, which is correct and merely larger -- 2.5 MB for
    ~3700 transactions.

    Kept because `build_payload(since=...)` is still the right primitive, and the diagnostics show how
    much a device would have skipped.
    """
    from src.models import AppMetadata

    row = db.get(AppMetadata, _OWN_WATERMARK_KEY)
    return coding.datetime_from_wire(row.value) if row else None


def _record_own_publish(
    db: Session, device_id: str, label: str, platform_name: str, payload: dict
) -> None:
    from src.models import AppMetadata

    watermark = payload.get("watermark")
    if watermark:
        row = db.get(AppMetadata, _OWN_WATERMARK_KEY)
        if row is None:
            db.add(AppMetadata(key=_OWN_WATERMARK_KEY, value=watermark))
        elif (row.value or "") < watermark:
            row.value = watermark

    # This device appears in its own registry, so the UI can show "this Mac" alongside the peers.
    device.remember_device(
        db,
        device_id,
        label=label,
        platform_name=platform_name,
        last_seen_at=coding.datetime_from_wire(payload.get("written_at")),
    )


# --- the Plaid economy ------------------------------------------------------------------------

PLAID_LAST_PULL_KEY = "plaid_last_pull_at"


def _plaid_report(db: Session) -> dict[str, Any]:
    """What this device tells peers about its Plaid usage: a timestamp, and nothing else.

    Explicitly not the cursor, the item id or the access token. `sync_log` holds those and is
    excluded from payloads; publishing a cursor would also let two devices fight over one position.
    """
    from src.models import AppMetadata

    row = db.get(AppMetadata, PLAID_LAST_PULL_KEY)
    return {"last_pull_at": row.value if row else None}


def record_plaid_pull(db: Session, when: datetime | None = None) -> None:
    """Note that this device just pulled from Plaid, so peers can skip doing it again."""
    from src.models import AppMetadata

    value = coding.datetime_to_wire(when or datetime.utcnow())
    row = db.get(AppMetadata, PLAID_LAST_PULL_KEY)
    if row is None:
        db.add(AppMetadata(key=PLAID_LAST_PULL_KEY, value=value))
    else:
        row.value = value
    db.commit()


def last_plaid_pull_anywhere(db: Session, transport: SyncTransport | None = None) -> datetime | None:
    """The most recent Plaid pull by *any* device, as far as this device knows.

    Read from peers' already-merged payloads where available, and otherwise from the registry, so
    this stays cheap: it must not itself make a network call in order to decide whether to avoid one.
    """
    from src.models import AppMetadata

    candidates: list[datetime] = []
    row = db.get(AppMetadata, PLAID_LAST_PULL_KEY)
    mine = coding.datetime_from_wire(row.value) if row else None
    if mine:
        candidates.append(mine)

    for reported in _peer_plaid_reports(db):
        candidates.append(reported)

    return max(candidates) if candidates else None


_PEER_PLAID_KEY_PREFIX = "plaid_peer_pull_at:"


def note_peer_plaid_report(db: Session, device_id: str, payload: dict) -> None:
    """Remember a peer's reported Plaid pull time, so the decision needs no network."""
    from src.models import AppMetadata

    reported = (payload.get("plaid") or {}).get("last_pull_at")
    if not reported:
        return
    key = f"{_PEER_PLAID_KEY_PREFIX}{device_id}"
    row = db.get(AppMetadata, key)
    if row is None:
        db.add(AppMetadata(key=key, value=reported))
    elif (row.value or "") < reported:
        row.value = reported


def _peer_plaid_reports(db: Session) -> list[datetime]:
    from src.models import AppMetadata

    rows = (
        db.query(AppMetadata)
        .filter(AppMetadata.key.like(f"{_PEER_PLAID_KEY_PREFIX}%"))
        .all()
    )
    out: list[datetime] = []
    for row in rows:
        parsed = coding.datetime_from_wire(row.value)
        if parsed is not None:
            out.append(parsed)
    return out


def plaid_pull_is_needed(
    db: Session, *, interval: timedelta, now: datetime | None = None
) -> tuple[bool, str]:
    """Whether to spend a Plaid call, and the reason -- suitable for a log line.

    The question is "has anybody pulled recently", not "have I pulled recently". That is what makes
    adding a phone cost nothing extra in Plaid calls.

    A peer's report older than `PLAID_REPORT_TRUST_WINDOW` is ignored: a device that has been offline
    for days should not keep a live device from refreshing on the strength of an ancient claim.
    """
    now = now or datetime.utcnow()
    latest = last_plaid_pull_anywhere(db)
    if latest is None:
        return True, "no device has recorded a Plaid pull"
    age = now - latest
    if age > PLAID_REPORT_TRUST_WINDOW:
        return True, f"the newest report is {age} old, beyond the trust window"
    if age >= interval:
        return True, f"the last pull anywhere was {age} ago"
    return False, f"another device pulled {age} ago; skipping to avoid a duplicate Plaid call"
