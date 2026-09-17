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

from src.sync import coding, device, schema
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
    #: True when publishing was skipped because nothing had changed -- distinct from a failure.
    skipped_publish: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "peers_seen": self.peers_seen,
            "peers_failed": self.peers_failed,
            "merges": self.merges,
            "published": self.published,
            "skipped_publish": self.skipped_publish,
            "error": self.error,
        }


def run_sync(
    db: Session,
    transport: SyncTransport,
    *,
    device_id: str | None = None,
    device_label: str | None = None,
    full: bool = False,
    force_publish: bool = False,
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
            # Recorded whether or not the payload is merged. Knowing a peer exists is what lets the
            # UI offer the first-merge choice at all, and the lease timestamps are timestamps --
            # not the user's history -- so withholding them would only make both devices spend
            # Plaid calls and take duplicate backups while waiting for an answer.
            last_seen = remote.modified_at or coding.datetime_from_wire(
                remote.payload.get("written_at")
            )
            device.remember_device(
                db,
                remote.device_id,
                label=remote.payload.get("device_label"),
                platform_name=remote.payload.get("platform"),
                # The store's own timestamp where it has one: a peer's self-reported clock can be
                # wrong, and this is used to judge freshness.
                last_seen_at=last_seen,
            )
            note_peer_plaid_report(db, remote.device_id, remote.payload)
            note_peer_backup_report(db, remote.device_id, remote.payload)
            # Committed before the preview below, which rolls back to discard the trial merge and
            # would otherwise take this bookkeeping with it.
            db.commit()

            if not merge_consent_given(db):
                # First contact. Count what the payload *would* do and stop -- absorbing another
                # device's entire history unasked is how a tracker loses trust, and because
                # tombstones only cover deletions made after sync shipped, a blind first merge
                # produces the *union* of two databases and resurrects anything either side deleted
                # beforehand. Publishing continues, so this device is not invisible while it waits.
                preview = preview_merge(db, remote.payload)
                record_pending_first_merge(db, remote.device_id, preview)
                result.merges[remote.device_id] = {"awaiting_consent": True, **preview}
                db.commit()
                continue

            report = merge_payload(db, remote.payload)
            result.merges[remote.device_id] = report.as_dict()
            device.remember_device(
                db,
                remote.device_id,
                label=remote.payload.get("device_label"),
                platform_name=remote.payload.get("platform"),
                last_seen_at=last_seen,
                watermark=report.watermark,
            )
            db.commit()
        except Exception as exc:
            # One bad payload must not stop syncing with every other device.
            logger.exception("sync: merging the payload from %s failed", remote.device_id)
            result.peers_failed.append(remote.device_id)
            result.merges[remote.device_id] = {"error": str(exc)}
            db.rollback()

    # Payloads are full state, so republishing an unchanged one uploads the entire history for
    # nothing -- ~2.5 MB per round, which at a 15-minute poll is a few hundred MB a day of pointless
    # traffic. Skip when neither our own data nor anything we just merged has changed.
    # Computed *after* merging, so anything just learned from a peer is included and gets relayed
    # onward. An explicit "did the merge change something?" check was here too and was removed as
    # provably redundant: every synced model declares `onupdate` on `updated_at`, so any ORM
    # modification moves that row's timestamp, and inserts and deletes move the row count -- both of
    # which the signature covers. Verified by mutation: with the check removed, no test changes
    # behaviour, including one written specifically for uid convergence, which touches no timestamp
    # of its own.
    #
    # The signature alone is not enough to justify skipping. It records what this device last
    # *decided* to publish, not what is actually on the transport, so if the payload is removed --
    # a trashed file, a folder someone cleared, a fresh Drive account -- the signature still
    # matches and the device never republishes. Observed live: a container reporting
    # `skipped_publish: true` with no payload on Drive at all, which is sync silently doing
    # nothing until local data happens to change. So confirm the payload is still there before
    # trusting the fingerprint.
    signature = _content_signature(db)
    if not force_publish and signature == _stored_signature(db) and _payload_still_published(
        transport, device_id, result
    ):
        result.published = False
        result.skipped_publish = True
        return result

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
        payload["backup"] = _backup_report(db)
        transport.put(device_id, payload)
        _record_own_publish(db, device_id, device_label, platform_name, payload)
        _store_signature(db, signature)
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


# --- publish-skipping ---------------------------------------------------------------------------

_SIGNATURE_KEY = "sync_last_published_signature"


def _content_signature(db: Session) -> str:
    """A cheap fingerprint of everything that would go into a payload.

    Per synced table: row count, newest `updated_at`, **and the sum of all `updated_at` values**.
    Not a hash of the payload itself -- building one is the expensive step this exists to avoid.

    The sum is the part that makes this safe, and leaving it out was a real bug. Count-and-maximum
    misses the most ordinary edit there is: re-categorising an *old* transaction moves that row's
    `updated_at` but changes neither the row count nor the table maximum, so the signature looked
    unchanged and the edit was **never published at all**. Caught by
    `test_a_merged_update_relays_even_when_the_signature_does_not_move`, which failed on the wrong
    side -- the peer never published, rather than the relay being skipped.

    Any change to any row's timestamp moves the sum. Two edits cancelling out exactly would defeat it,
    which requires one row's timestamp to move backwards by precisely what another moved forward; not
    a thing monotonic clocks do.
    """
    from sqlalchemy import func

    from src.models import Tombstone

    parts: list[str] = []
    for spec in schema.TABLES:
        model = schema.model_for(spec)
        count, newest, total = db.query(
            func.count(model.updated_at),
            func.max(model.updated_at),
            # strftime rather than arithmetic on the column: these are stored as datetime strings.
            func.sum(func.strftime("%s", model.updated_at)),
        ).one()
        parts.append(f"{spec.name}:{count}:{newest}:{total}")
    parts.append(f"tombstones:{db.query(func.count(Tombstone.ref)).scalar()}")
    return "|".join(parts)


def _stored_signature(db: Session) -> str | None:
    from src.models import AppMetadata

    row = db.get(AppMetadata, _SIGNATURE_KEY)
    return row.value if row else None


def _store_signature(db: Session, signature: str) -> None:
    from src.models import AppMetadata

    row = db.get(AppMetadata, _SIGNATURE_KEY)
    if row is None:
        db.add(AppMetadata(key=_SIGNATURE_KEY, value=signature))
    else:
        row.value = signature


def _payload_still_published(
    transport: SyncTransport, device_id: str, result: SyncResult
) -> bool:
    """Whether this device's own payload is present on the transport.

    A transport that cannot answer is treated as "present": the fallback must be to skip a
    redundant 2.5 MB upload rather than to re-upload on every poll because a listing failed.
    Publishing is retried on the next round anyway, and the round after that.
    """
    lister = getattr(transport, "has_payload", None)
    if callable(lister):
        try:
            return bool(lister(device_id))
        except Exception:
            logger.warning("sync: could not confirm our payload is still published", exc_info=True)
            return True
    return True


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


# --- the backup lease -------------------------------------------------------------------------
#
# Same shape as the Plaid economy above, and for the same reason: the question is "has *anybody*
# backed up recently", not "have I". Every device holds the whole database, so a snapshot taken on
# the Mac protects the container equally -- and without this, N devices produce N near-identical
# snapshots a day and the retention window shrinks to a fraction of the days it claims to cover.
#
# A lease, not a lock. If two devices decide to back up at once the result is two dated files that
# are both valid; wasteful, never wrong. That is why no coordination is attempted.

BACKUP_LAST_AT_KEY = "backup_last_at"
_PEER_BACKUP_KEY_PREFIX = "backup_peer_at:"


def _backup_report(db: Session) -> dict[str, Any]:
    """What this device tells peers about backups: a timestamp, and nothing else."""
    from src.models import AppMetadata

    row = db.get(AppMetadata, BACKUP_LAST_AT_KEY)
    return {"last_backup_at": row.value if row else None}


def record_backup(db: Session, when: datetime | None = None) -> None:
    """Note that this device just took a snapshot, so peers can skip taking their own."""
    from src.models import AppMetadata

    value = coding.datetime_to_wire(when or datetime.utcnow())
    row = db.get(AppMetadata, BACKUP_LAST_AT_KEY)
    if row is None:
        db.add(AppMetadata(key=BACKUP_LAST_AT_KEY, value=value))
    else:
        row.value = value
    db.commit()


def note_peer_backup_report(db: Session, device_id: str, payload: dict) -> None:
    """Remember a peer's reported backup time, so the decision needs no network."""
    from src.models import AppMetadata

    reported = (payload.get("backup") or {}).get("last_backup_at")
    if not reported:
        return
    key = f"{_PEER_BACKUP_KEY_PREFIX}{device_id}"
    row = db.get(AppMetadata, key)
    if row is None:
        db.add(AppMetadata(key=key, value=reported))
    elif (row.value or "") < reported:
        row.value = reported


def last_backup_anywhere(db: Session) -> datetime | None:
    """The most recent snapshot by *any* device, as far as this device knows."""
    from src.models import AppMetadata

    candidates: list[datetime] = []
    row = db.get(AppMetadata, BACKUP_LAST_AT_KEY)
    mine = coding.datetime_from_wire(row.value) if row else None
    if mine:
        candidates.append(mine)
    for peer_row in (
        db.query(AppMetadata).filter(AppMetadata.key.like(f"{_PEER_BACKUP_KEY_PREFIX}%")).all()
    ):
        parsed = coding.datetime_from_wire(peer_row.value)
        if parsed is not None:
            candidates.append(parsed)
    return max(candidates) if candidates else None


# --- first-contact consent ----------------------------------------------------------------------
#
# Modelled on Timeslice, which asks once before absorbing another device's history and, until it is
# answered, publishes but does not merge. Two reasons it matters more here: a finance database is
# not reconstructible from memory, and tombstones only cover deletions made after sync shipped, so
# a first merge between two long-lived databases produces their *union* -- resurrecting anything
# either side deleted beforehand.

MERGE_CONSENT_KEY = "sync_merge_consent"
PENDING_FIRST_MERGE_KEY = "sync_pending_first_merge"


def merge_consent_given(db: Session) -> bool:
    from src.models import AppMetadata

    row = db.get(AppMetadata, MERGE_CONSENT_KEY)
    return bool(row and (row.value or "").strip() not in ("", "0", "false", "no"))


def grant_merge_consent(db: Session) -> None:
    """Answered yes. From now on peers merge normally, and the prompt does not return."""
    from src.models import AppMetadata

    row = db.get(AppMetadata, MERGE_CONSENT_KEY)
    if row is None:
        db.add(AppMetadata(key=MERGE_CONSENT_KEY, value="1"))
    else:
        row.value = "1"
    db.query(AppMetadata).filter(AppMetadata.key == PENDING_FIRST_MERGE_KEY).delete()
    db.commit()


def preview_merge(db: Session, payload: dict) -> dict[str, Any]:
    """What merging this payload would change, without changing it.

    Runs the real merge and rolls it back, so the counts come from the code that would actually
    run rather than from a second implementation that can drift from it.
    """
    import json as _json

    from src.sync.merge import merge_payload as _merge

    try:
        report = _merge(db, payload, dry_run=True)
        counts = report.as_dict()
    finally:
        # dry_run leaves the work uncommitted; this is what discards it.
        db.rollback()
    inserted = counts.get("inserted") or {}
    updated = counts.get("updated") or {}
    return {
        "device_id": payload.get("device_id"),
        "device_label": payload.get("device_label"),
        "would_insert": sum(inserted.values()) if isinstance(inserted, dict) else 0,
        "would_update": sum(updated.values()) if isinstance(updated, dict) else 0,
        "by_table": {
            table: {"insert": inserted.get(table, 0), "update": updated.get(table, 0)}
            for table in sorted(set(inserted) | set(updated))
        },
        "detail": _json.loads(_json.dumps(counts, default=str)),
    }


def record_pending_first_merge(db: Session, device_id: str, preview: dict) -> None:
    """Stash the preview so the UI can show real numbers, not "a peer exists"."""
    import json as _json

    from src.models import AppMetadata

    value = _json.dumps(preview, default=str)
    row = db.get(AppMetadata, PENDING_FIRST_MERGE_KEY)
    if row is None:
        db.add(AppMetadata(key=PENDING_FIRST_MERGE_KEY, value=value))
    else:
        row.value = value


def pending_first_merge(db: Session) -> dict | None:
    import json as _json

    from src.models import AppMetadata

    if merge_consent_given(db):
        return None
    row = db.get(AppMetadata, PENDING_FIRST_MERGE_KEY)
    if not row or not row.value:
        return None
    try:
        return _json.loads(row.value)
    except ValueError:
        return None
