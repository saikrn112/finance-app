"""Drop tombstones for rows that exist again.

A tombstone asserts "this row is deleted". If the row is present locally, that assertion is false,
and it is not harmless: the merge honours the tombstone, so every peer deletes the row on its next
round. Locally nothing looks wrong -- the row is right there -- which is why this is invisible until
a second device exists.

They arose because re-creating a row did not retract its own deletion. The splits route removes a
transaction's split rows and re-inserts them under the same identity on every edit, so each edit left
a tombstone behind. That leak is fixed in `src/sync/tracking.py`; this clears what it already left.

Preview first, apply second, and idempotent. CLI-only: a repair that runs itself on every boot is a
repair nobody reviews.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.sync.refs import RefResolver
from src.sync.tracking import pause_tombstones, resume_tombstones

logger = logging.getLogger(__name__)


def _live_refs(db: Session) -> dict[tuple[str, str], int]:
    """Every (kind, ref) that currently exists locally, with how many rows claim it."""
    from src.sync import schema

    counts: dict[tuple[str, str], int] = {}
    resolver = RefResolver(db)
    for spec in schema.TABLES:
        for row in db.query(schema.model_for(spec)).all():
            described = resolver.describe(row)
            if described:
                counts[described] = counts.get(described, 0) + 1
    return counts


def preview_retraction(db: Session) -> dict[str, Any]:
    """Tombstones contradicted by a row that exists. Writes nothing."""
    from src.models import Tombstone

    live = _live_refs(db)
    stale = [
        t for t in db.query(Tombstone).all() if (t.kind, t.ref) in live
    ]
    by_kind: dict[str, int] = {}
    for t in stale:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
    return {
        "tombstones_total": db.query(Tombstone).count(),
        "contradicted": len(stale),
        "by_kind": by_kind,
        "examples": [f"{t.kind}:{t.ref[:80]}" for t in stale[:5]],
    }


def run_retraction(db: Session) -> dict[str, Any]:
    """Delete the contradicted tombstones. Idempotent: a second run reports zero."""
    from src.models import Tombstone

    before = preview_retraction(db)
    live = _live_refs(db)
    # Paused: deleting a Tombstone row must not itself be recorded as a deletion to propagate.
    pause_tombstones()
    try:
        removed = 0
        for t in db.query(Tombstone).all():
            if (t.kind, t.ref) in live:
                db.delete(t)
                removed += 1
        db.commit()
    finally:
        resume_tombstones()
    after = preview_retraction(db)
    logger.info("retracted %d contradicted tombstone(s)", removed)
    return {
        "retracted": removed,
        "expected": before["contradicted"],
        "remaining": after["contradicted"],
        "tombstones_left": after["tombstones_total"],
    }
