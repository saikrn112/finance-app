"""Building this device's payload.

One file per device, so no two devices ever write the same object and file-level conflicts cannot
occur. The payload is this device's view of the synced tables; the receiver applies it record by
record.

## What is deliberately absent

No secrets. `sync_log` holds Plaid access tokens and per-item cursors and is excluded entirely, as is
`config.yaml`. The vault archive contains both because restoring is "put my own machine back"; a sync
payload is a file sitting in cloud storage being read by other devices, which is a different threat
model. `assert_no_secrets` enforces this rather than trusting the table list.

## Watermarks

`since` limits the payload to rows changed after a timestamp, so a steady state does not re-upload
the whole history on every poll -- the flaw that makes Timeslice's payloads grow without bound.

Tombstones are **always sent in full**. They are small, and a tombstone missed because of a watermark
is a row that comes back, which is exactly the failure the whole mechanism exists to prevent.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.sync import coding, refs, schema
from src.sync.refs import RefResolver

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

#: Tables that must never appear in a payload, and why. Checked, not just documented.
FORBIDDEN_TABLES = {
    "sync_log": "holds Plaid access tokens and per-item cursors",
    "plaid_api_usage": "per-device attribution; sharing it would misattribute quota",
    "app_metadata": "device-local (database_instance_id, counters)",
    "exchange_rates": "regenerable from the provider; wastes payload",
    "feedback": "device-local notes, not shared history",
    "feedback_attachments": "device-local",
    "splitwise_commits": "records what this device pushed",
    "connected_accounts": "derived from sync_log, which is excluded",
}

#: Substrings that must not appear anywhere in a serialised payload.
_SECRET_KEY_HINTS = ("access_token", "client_secret", "refresh_token", "plaid_cursor", "api_key")


def build_payload(
    db: Session,
    *,
    device_id: str,
    device_label: str | None = None,
    platform: str | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    """This device's payload. Reads only."""
    resolver = RefResolver(db)
    records: dict[str, list[dict[str, Any]]] = {}
    skipped_unnameable = 0

    for spec in schema.TABLES:
        model = schema.model_for(spec)
        query = db.query(model)
        if since is not None:
            # A row with no updated_at has never been backfilled; include it rather than hiding it,
            # because "unknown age" must not mean "invisible to every peer forever".
            query = query.filter(
                (model.updated_at.is_(None)) | (model.updated_at > since)
            )
        rows: list[dict[str, Any]] = []
        for instance in query.all():
            described = resolver.describe(instance)
            if described is None:
                skipped_unnameable += 1
                continue
            _kind, ref = described
            record: dict[str, Any] = {
                "ref": ref,
                "updated_at": coding.datetime_to_wire(getattr(instance, "updated_at", None)),
            }
            for field in spec.all_fields:
                record[field.name] = field.to_wire(getattr(instance, field.attribute, None))
            rows.append(record)
        if rows:
            records[spec.name] = rows

    from src.models import Tombstone

    tombstones = [
        {
            "kind": row.kind,
            "ref": row.ref,
            "deleted_at": coding.datetime_to_wire(row.deleted_at),
        }
        for row in db.query(Tombstone).all()
    ]

    payload = {
        "format_version": FORMAT_VERSION,
        "device_id": device_id,
        "device_label": device_label,
        "platform": platform,
        "written_at": coding.datetime_to_wire(datetime.utcnow()),
        "since": coding.datetime_to_wire(since),
        "watermark": _watermark(records),
        "records": records,
        "tombstones": tombstones,
    }

    if skipped_unnameable:
        logger.warning(
            "sync: %d row(s) had no natural reference and were left out of the payload",
            skipped_unnameable,
        )
    assert_no_secrets(payload)
    return payload


def _watermark(records: dict[str, list[dict[str, Any]]]) -> str | None:
    """The newest `updated_at` in this payload, for the receiver to store as its high-water mark.

    Computed from what is actually included rather than from `now`: a clock ahead of the data would
    make the peer skip rows it has never seen.
    """
    newest: str | None = None
    for rows in records.values():
        for row in rows:
            value = row.get("updated_at")
            if value and (newest is None or value > newest):
                newest = value
    return newest


def assert_no_secrets(payload: dict[str, Any]) -> None:
    """Fail loudly if a payload contains anything that must not leave the device.

    A belt-and-braces check on top of the table list, because the cost of being wrong is publishing
    a Plaid access token to cloud storage. Cheap: a substring scan of one JSON dump.
    """
    import json

    for table in payload.get("records", {}):
        if table in FORBIDDEN_TABLES:
            raise AssertionError(
                f"sync payload contains the excluded table {table!r}: {FORBIDDEN_TABLES[table]}"
            )

    serialised = json.dumps(payload, default=str).lower()
    for hint in _SECRET_KEY_HINTS:
        if hint in serialised:
            raise AssertionError(
                f"sync payload appears to contain a secret ({hint!r}); refusing to publish it"
            )


def find_unsyncable(db: Session) -> dict[str, int]:
    """Rows that cannot be named, and therefore cannot sync, per table.

    Found on the first run against real data: 61 link rows pointed at transactions that no longer
    existed. SQLite does not enforce foreign keys unless asked, so a past delete or re-import left
    them dangling. They are not merely unsyncable -- they are invisible in the app too, because every
    read joins through the parent they have lost.

    Worth surfacing rather than logging: silently dropping rows from a payload is exactly the kind of
    difference between two devices that nobody notices until the numbers disagree.
    """
    resolver = RefResolver(db)
    counts: dict[str, int] = {}
    for spec in schema.TABLES:
        model = schema.model_for(spec)
        unnameable = sum(
            1 for instance in db.query(model).all() if resolver.describe(instance) is None
        )
        if unnameable:
            counts[spec.name] = unnameable
    return counts


def payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Counts only -- safe to log. Never log a payload: it is the user's financial history."""
    return {
        "device_id": payload.get("device_id"),
        "records": {table: len(rows) for table, rows in payload.get("records", {}).items()},
        "tombstones": len(payload.get("tombstones", [])),
        "watermark": payload.get("watermark"),
    }
