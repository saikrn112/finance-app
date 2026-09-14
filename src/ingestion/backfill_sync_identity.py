"""Populate the multi-device sync identity columns on an existing database.

`init_db` adds the columns; this mints their values. Separate on purpose: adding a column touches no
rows, while this rewrites real financial history, and startup backfill was removed from this project
precisely so that cannot happen on every boot.

## Why every value here is derived, never random or "now"

Two devices will each run this against their own copy of the same history. If the values they mint
differ, the merge sees two *different* records where there is really one, and either duplicates the
row or picks a spurious last-write-wins winner.

So:

* **uid is derived from content**, not `uuid4`. Two devices backfilling the same project independently
  arrive at the same uid, so it merges instead of duplicating. Rows created *after* sync exists can
  use random uids safely -- by then the row is created once and travels.
* **updated_at is derived from an existing timestamp**, never from the clock at backfill time. Seeding
  it with `now` would make whichever device backfilled *later* win every field comparison, silently
  overwriting the other device's genuine older edits with identical-looking stale values.

Link tables have no timestamp of their own, so they inherit their parent's `created_at`: a link
cannot predate the transaction or project it points at, and both devices compute it identically.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

# Bump when the derivation rules below change, so a re-run can be reasoned about: a uid minted under
# v1 rules is not the uid v2 would mint, and mixing them across devices would not converge.
DERIVATION_VERSION = "v1"


def _derived_uid(kind: str, *parts: object) -> str:
    """A stable uid for a row that existed before sync did.

    Namespaced by `kind` so a project and a contact of the same name cannot collide, and versioned
    so the rules can change without silently producing a second identity for the same row.
    """
    material = "|".join(["finance-sync", DERIVATION_VERSION, kind, *(str(p) for p in parts)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


# (table, uid kind, columns whose values derive the uid, column to copy updated_at from)
#
# The uid material is the row's *identifying* content, not everything in it: `projects.name` is
# unique, so it identifies the project, while colour and budget are values that may legitimately
# differ between devices and must not change identity.
_UID_TABLES = [
    ("projects", "project", ["name"], "created_at"),
    ("contacts", "contact", ["name"], "created_at"),
    # No natural key on these two. Their identifying content is the whole rule/subscription, because
    # two rows differing in any of these fields really are different rules.
    ("rules", "rule", ["pattern", "match_field", "category"], "created_at"),
    ("subscriptions", "subscription", ["merchant", "frequency", "source"], "created_at"),
]

# Tables that already have a natural key and need no uid, but whose `updated_at` may be NULL on old
# rows. Seeding it matters more than it looks: a NULL age means the receiving device has nothing to
# stamp the row with, and the model default then claims the row was edited *now* -- which wins every
# later comparison and can overwrite a genuine annotation on another device with a stale one.
# Measured on the real database: 2427 transactions had a NULL updated_at.
_TIMESTAMP_ONLY_TABLES = [
    ("transactions", "created_at"),
    ("account_activity", "created_at"),
]

# Link tables: updated_at only, inherited from the parent named here.
_LINK_TABLES = [
    ("transaction_projects", "transactions", "transaction_id"),
    ("transaction_splits", "transactions", "transaction_id"),
    ("transaction_project_splits", "transactions", "transaction_id"),
    ("project_members", "projects", "project_id"),
]


def _table_exists(db: Session, table: str) -> bool:
    row = db.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table}
    ).first()
    return row is not None


def _columns(db: Session, table: str) -> set[str]:
    return {r[1] for r in db.execute(text(f"PRAGMA table_info({table})"))}


def preview_backfill(db: Session) -> dict:
    """How many rows *would* be written, per table. Reads only."""
    counts: dict[str, dict[str, int]] = {}
    for table, _kind, _material, _source in _UID_TABLES:
        if not _table_exists(db, table):
            continue
        cols = _columns(db, table)
        entry: dict[str, int] = {}
        if "uid" in cols:
            entry["uid"] = db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE uid IS NULL")
            ).scalar_one()
        if "updated_at" in cols:
            entry["updated_at"] = db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE updated_at IS NULL")
            ).scalar_one()
        if entry:
            counts[table] = entry

    for table, _source in _TIMESTAMP_ONLY_TABLES:
        if not _table_exists(db, table) or "updated_at" not in _columns(db, table):
            continue
        pending = db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE updated_at IS NULL")
        ).scalar_one()
        if pending:
            counts[table] = {"updated_at": pending}

    for table, _parent, _fk in _LINK_TABLES + [("contact_splitwise_links", "", "")]:
        if not _table_exists(db, table) or "updated_at" not in _columns(db, table):
            continue
        pending = db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE updated_at IS NULL")
        ).scalar_one()
        if pending:
            counts[table] = {"updated_at": pending}

    return {
        "derivation_version": DERIVATION_VERSION,
        "pending": counts,
        "total_rows": sum(sum(v.values()) for v in counts.values()),
    }


def run_backfill(db: Session) -> dict:
    """Mint the missing values. Idempotent: only ever touches NULLs."""
    written: dict[str, dict[str, int]] = {}

    for table, kind, material_columns, source_column in _UID_TABLES:
        if not _table_exists(db, table):
            continue
        cols = _columns(db, table)
        entry: dict[str, int] = {}

        if "uid" in cols:
            selected = ", ".join(["id", *material_columns])
            rows = db.execute(text(f"SELECT {selected} FROM {table} WHERE uid IS NULL")).all()
            minted = 0
            for row in rows:
                uid = _derived_uid(kind, *row[1:])
                # A uid can already be taken when two pre-sync rows derive the same identity -- two
                # rules with the same pattern, field and category, which really are the same rule
                # duplicated. Leave the later row NULL rather than violating the unique index; it is
                # visible in the preview count and is a data-cleanup question, not a sync one.
                clash = db.execute(
                    text(f"SELECT 1 FROM {table} WHERE uid = :uid"), {"uid": uid}
                ).first()
                if clash:
                    continue
                db.execute(
                    text(f"UPDATE {table} SET uid = :uid WHERE id = :id AND uid IS NULL"),
                    {"uid": uid, "id": row[0]},
                )
                minted += 1
            entry["uid"] = minted

        if "updated_at" in cols and source_column in cols:
            # COALESCE so a row with no created_at still gets a value; the epoch is a deterministic
            # "unknown, and older than anything real", which is the safe direction for LWW.
            result = db.execute(
                text(
                    f"UPDATE {table} SET updated_at = COALESCE({source_column}, :epoch) "
                    "WHERE updated_at IS NULL"
                ),
                {"epoch": _EPOCH},
            )
            entry["updated_at"] = result.rowcount or 0

        if entry:
            written[table] = entry

    for table, source_column in _TIMESTAMP_ONLY_TABLES:
        if not _table_exists(db, table):
            continue
        cols = _columns(db, table)
        if "updated_at" not in cols or source_column not in cols:
            continue
        result = db.execute(
            text(
                f"UPDATE {table} SET updated_at = COALESCE({source_column}, :epoch) "
                "WHERE updated_at IS NULL"
            ),
            {"epoch": _EPOCH},
        )
        if result.rowcount:
            written[table] = {"updated_at": result.rowcount}

    for table, parent, fk in _LINK_TABLES:
        if not _table_exists(db, table) or "updated_at" not in _columns(db, table):
            continue
        result = db.execute(
            text(
                f"UPDATE {table} SET updated_at = COALESCE("
                f"  (SELECT p.created_at FROM {parent} p WHERE p.id = {table}.{fk}), :epoch"
                ") WHERE updated_at IS NULL"
            ),
            {"epoch": _EPOCH},
        )
        if result.rowcount:
            written[table] = {"updated_at": result.rowcount}

    # This one has its own timestamp already.
    if _table_exists(db, "contact_splitwise_links"):
        cols = _columns(db, "contact_splitwise_links")
        if "updated_at" in cols and "linked_at" in cols:
            result = db.execute(
                text(
                    "UPDATE contact_splitwise_links "
                    "SET updated_at = COALESCE(linked_at, :epoch) WHERE updated_at IS NULL"
                ),
                {"epoch": _EPOCH},
            )
            if result.rowcount:
                written["contact_splitwise_links"] = {"updated_at": result.rowcount}

    db.commit()
    return {
        "derivation_version": DERIVATION_VERSION,
        "written": written,
        "total_rows": sum(sum(v.values()) for v in written.values()),
        "remaining": preview_backfill(db)["total_rows"],
    }


# Deterministic and unambiguously older than any real edit.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
