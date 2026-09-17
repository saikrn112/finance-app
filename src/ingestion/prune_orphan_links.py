"""Remove join rows that point at transactions which no longer exist.

These accumulated because deleting a transaction did not delete its links: SQLite declares the
foreign keys `NO ACTION` and runs with `PRAGMA foreign_keys = 0`, so nothing refused the delete or
tidied up. That leak is fixed in `src/ingestion/plaid_sync.py`; this clears what it already left --
77 rows across eight projects on the install where it was found.

They are unreachable, not merely untidy. Every read joins through the parent transaction, so they
are invisible in the app, and `find_unsyncable` excludes them from every sync payload because a row
that cannot be named cannot be put on the wire. Deleting them therefore removes nothing a user can
see or a peer can receive.

Preview first, apply second, and idempotent: a second run finds nothing. Deliberately CLI-only and
never on startup, for the reason in AGENTS.md -- a repair that runs itself on every boot is a repair
nobody reviews.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Join tables keyed by transaction_id. Order is irrelevant -- nothing references these.
LINK_TABLES = (
    "transaction_projects",
    "transaction_splits",
    "transaction_project_splits",
)


def _orphan_rows(db: Session, table: str) -> list[tuple[str, int]]:
    return [
        (row[0], row[1])
        for row in db.execute(
            text(
                f"""
                SELECT l.transaction_id, COUNT(*)
                FROM {table} l
                LEFT JOIN transactions t ON t.id = l.transaction_id
                WHERE t.id IS NULL
                GROUP BY l.transaction_id
                """
            )
        ).fetchall()
    ]


def preview_prune(db: Session) -> dict[str, Any]:
    """What would be deleted, and which projects and people it touches. Writes nothing."""
    per_table: dict[str, int] = {}
    transactions: set[str] = set()
    for table in LINK_TABLES:
        rows = _orphan_rows(db, table)
        if rows:
            per_table[table] = sum(count for _, count in rows)
        transactions.update(tid for tid, _ in rows)

    by_project = {
        name: count
        for name, count in db.execute(
            text(
                """
                SELECT p.name, COUNT(*)
                FROM transaction_projects l
                JOIN projects p ON p.id = l.project_id
                LEFT JOIN transactions t ON t.id = l.transaction_id
                WHERE t.id IS NULL
                GROUP BY p.name
                ORDER BY 2 DESC
                """
            )
        ).fetchall()
    }

    return {
        "rows": per_table,
        "total_rows": sum(per_table.values()),
        "missing_transactions": len(transactions),
        "affected_projects": by_project,
    }


def run_prune(db: Session) -> dict[str, Any]:
    """Delete the orphaned rows. Idempotent: a second run reports zero."""
    before = preview_prune(db)
    deleted: dict[str, int] = {}
    for table in LINK_TABLES:
        result = db.execute(
            text(
                f"""
                DELETE FROM {table}
                WHERE transaction_id NOT IN (SELECT id FROM transactions)
                """
            )
        )
        if result.rowcount:
            deleted[table] = int(result.rowcount)
    db.commit()

    after = preview_prune(db)
    logger.info("pruned %d orphaned link row(s)", sum(deleted.values()))
    return {
        "deleted": deleted,
        "total_deleted": sum(deleted.values()),
        "expected": before["total_rows"],
        "remaining": after["total_rows"],
    }
