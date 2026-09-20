"""Recording deletions so they survive a merge.

## Why a hook rather than code at each delete site

A delete with no tombstone does not merely fail to propagate -- it comes back. The peer still holds
the row, sends it in its next payload, and the merge re-inserts it. To the user that reads as "delete
does not work", and for a transaction it reads as a wrong balance.

There are around twenty delete sites across the routes and the ingestion code, and more will be
added. Recording a tombstone at each one is a rule that has to be remembered every time, and the
failure is silent. So instead this listens to SQLAlchemy's `before_flush` and records tombstones for
every deleted instance of a synced model, wherever the delete was written.

## The gap this cannot close, and what to do about it

`before_flush` sees ORM deletes (`db.delete(obj)`). It does **not** see bulk deletes
(`db.query(X).filter(...).delete()`), which compile straight to `DELETE FROM` and never load the
rows -- so nothing knows which rows went. Those sites must call `bulk_delete` below, which loads the
rows first and then deletes them through the ORM.

`assert_no_untracked_bulk_deletes` is the guard: it scans the source for bulk deletes on synced
tables so a new one fails a test rather than silently losing deletions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session

from src.sync.refs import RefResolver

logger = logging.getLogger(__name__)

_enabled = True


def pause_tombstones() -> None:
    """Stop recording. Used by the merge itself: applying a peer's tombstone deletes local rows, and
    recording that as *our* deletion would be redundant, and would re-stamp `deleted_at` with our
    clock instead of keeping the originating device's."""
    global _enabled
    _enabled = False


def resume_tombstones() -> None:
    global _enabled
    _enabled = True


def tombstones_enabled() -> bool:
    return _enabled


def record_tombstone(db: Session, kind: str, ref: str, *, device_id: str | None = None) -> None:
    """Upsert one tombstone. Safe to call for a ref that already has one."""
    from src.models import Tombstone

    existing = db.get(Tombstone, {"kind": kind, "ref": ref})
    if existing is not None:
        return
    db.add(
        Tombstone(
            kind=kind,
            ref=ref,
            deleted_at=datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
            device_id=device_id,
        )
    )


def retract_tombstone(db: Session, kind: str, ref: str) -> int:
    """Forget a deletion, because the row it named exists again.

    The mirror of `record_tombstone`. A tombstone means "this is gone"; once the row is back, the
    claim is false, and leaving it in place makes every peer delete the row on their next merge.
    """
    from src.models import Tombstone

    # Deleted through the ORM, with autoflush off, because this runs inside `before_flush`. A bulk
    # `query.delete()` there emits its DELETE immediately and re-enters the flush, which corrupts
    # the unit of work -- it surfaced as "Error binding parameter: type 'UOWTransaction' is not
    # supported" on almost every test.
    removed = 0
    with db.no_autoflush:
        for row in (
            db.query(Tombstone)
            .filter(Tombstone.kind == kind, Tombstone.ref == ref)
            .all()
        ):
            db.delete(row)
            removed += 1
    return removed


@event.listens_for(Session, "before_flush")
def _record_tombstones_for_deletes(session: Session, _flush_context, _instances) -> None:
    """Record a tombstone for every synced row being deleted in this flush.

    Runs before the flush so the parents a link row is named after are still readable: naming
    `(transaction, project)` requires looking both up, and after the DELETE they may be gone.
    """
    if not _enabled or not (session.deleted or session.new):
        return

    try:
        # Autoflush off for the whole hook. Naming a row requires reading its parents, and a query
        # issued here would autoflush the very inserts this flush is about -- re-entering the flush
        # and corrupting the unit of work. It surfaced as "Error binding parameter: type
        # 'UOWTransaction' is not supported" at the setup of almost every test.
        with session.no_autoflush:
            _track_flush(session)
    except Exception:
        # A failure here must never block the user's delete. Losing propagation is recoverable;
        # a route that 500s on every delete is not.
        logger.exception("sync: failed to record tombstones for this flush")


def _track_flush(session: Session) -> None:
    try:
        resolver = RefResolver(session)

        # A row created again retracts its own deletion. Without this, editing a split deletes it
        # on every peer: the splits route removes a transaction's split rows and re-inserts them
        # under the same identity, so the delete leaves a tombstone that the re-insert never
        # clears, and the next merge obeys the tombstone. Nothing local looks wrong -- the row is
        # right there -- which is why this only surfaces once a peer exists.
        #
        # Only for local work. During a merge tombstone recording is paused anyway, and a peer's
        # insert must not silently revoke a deletion this device has not yet propagated.
        recreated, _ = resolver.describe_all(session.new)
        for kind, ref in recreated:
            retract_tombstone(session, kind, ref)

        described, unresolvable = resolver.describe_all(session.deleted)
        for kind, ref in described:
            record_tombstone(session, kind, ref)
        if unresolvable:
            # Worth a log rather than an exception: refusing the delete would be worse than losing
            # its propagation, and the count tells us the naming rules have a hole.
            logger.warning(
                "sync: %d deleted row(s) could not be named, so their deletion will not propagate",
                unresolvable,
            )
    except Exception:
        # A failure here must never block the user's delete. Losing propagation is recoverable;
        # a route that 500s on every delete is not.
        logger.exception("sync: failed to record tombstones for this flush")


def bulk_delete(db: Session, model, **filters) -> int:
    """Delete rows through the ORM so `before_flush` sees them, and return the count.

    Replaces `db.query(model).filter_by(**filters).delete()` at any site touching a synced table.
    Slower by one SELECT, which is the price of the deletion being expressible afterwards.

    **Flushes before returning, and must keep doing so.** `query.delete()` emits its DELETE
    immediately, and callers rely on that: the splits route deletes a transaction's splits and then
    re-inserts rows with the *same* composite primary key. Leaving the deletes pending puts a delete
    and an insert for one identity in the same unit of work, and the stale row wins -- which showed up
    as an unequal split refusing to go back to equal, with nothing raising.
    """
    rows = db.query(model).filter_by(**filters).all()
    if not rows:
        return 0
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)
