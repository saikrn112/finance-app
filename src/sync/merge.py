"""Applying a peer's payload.

## The property that matters

Merging is **idempotent** and **order-independent**: applying the same payload twice changes nothing
the second time, and applying A then B reaches the same state as B then A. That is what lets the
transport be dumb -- re-reads, retries and partial syncs are all safe -- and what lets devices
converge with no coordination, no leader and no server. Every rule below is chosen to preserve it,
which is why none of them depend on *when* a payload arrived or on which device is applying it.

## Order

1. **Tombstones first.** A deletion has to be able to beat an incoming row, and the row it deletes
   may be one this payload also contains.
2. **Contacts and projects**, which own their identity and are what links are named after.
3. **Rules and subscriptions.**
4. **Transactions.**
5. **Links**, which reference all of the above.

## Deletion vs a concurrent edit

A tombstone beats any record that is *not strictly newer* than the deletion. So the normal case -- a
peer that still holds the row and sends it back -- is filtered out, because that row's timestamp
predates the deletion. But a record edited *after* the delete happened is treated as a re-creation
and wins.

That last part is a deliberate departure from Timeslice, which lets delete win unconditionally.
Without it, deleting a project and later creating one of the same name is unsyncable: the tombstone
suppresses the new project forever. The cost is that a peer editing a row while another device
deletes it resurrects it -- which for a transaction restores real financial history, so it fails in
the safer direction.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.sync import coding, refs, schema
from src.sync.tracking import pause_tombstones, record_tombstone, resume_tombstones

logger = logging.getLogger(__name__)


@dataclass
class MergeReport:
    inserted: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped_older: dict[str, int] = field(default_factory=dict)
    skipped_deleted: dict[str, int] = field(default_factory=dict)
    #: A record whose parents are not present locally. Self-heals: payloads are full state, so the
    #: next merge retries once the parent has arrived.
    unresolved: dict[str, int] = field(default_factory=dict)
    #: A rename that would collide with a different local row of that name.
    rename_conflicts: int = 0
    deletions_applied: int = 0
    uid_converged: int = 0
    watermark: datetime | None = None

    def _bump(self, bucket: dict[str, int], table: str) -> None:
        bucket[table] = bucket.get(table, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_older": self.skipped_older,
            "skipped_deleted": self.skipped_deleted,
            "unresolved": self.unresolved,
            "rename_conflicts": self.rename_conflicts,
            "deletions_applied": self.deletions_applied,
            "uid_converged": self.uid_converged,
            "watermark": coding.datetime_to_wire(self.watermark),
        }

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.deletions_applied or self.uid_converged)


def merge_payload(db: Session, payload: dict[str, Any], *, dry_run: bool = False) -> MergeReport:
    """Apply `payload` to the local database. Idempotent.

    `dry_run` computes the report without committing, leaving the caller to roll back. It is a
    parameter rather than something a caller can arrange from outside because this function
    commits: wrapping it and rolling back afterwards silently does nothing, which is a mistake
    worth making impossible. Used by the first-contact preview, so the numbers shown to the user
    come from the code that would actually run.
    """
    version = payload.get("format_version")
    if version is not None and version > schema_format_version():
        # Newer peers may add fields; unknown ones are ignored below. A whole new *format* is not
        # something to guess at, so refuse rather than corrupt.
        raise ValueError(
            f"sync payload format {version} is newer than this build understands "
            f"({schema_format_version()})"
        )

    report = MergeReport()
    # Applying a peer's deletion must not record a tombstone of our own: it would restamp
    # `deleted_at` with our clock and make the same deletion look different on each device.
    pause_tombstones()
    try:
        _apply_tombstones(db, payload.get("tombstones") or [], report)
        deleted = _local_tombstones(db)

        for spec in schema.TABLES:
            rows = (payload.get("records") or {}).get(spec.name) or []
            for record in rows:
                try:
                    _apply_record(db, spec, record, deleted, report)
                except Exception:
                    logger.exception(
                        "sync: failed to apply a %s record; continuing with the rest", spec.name
                    )
        db.flush()
    finally:
        resume_tombstones()

    report.watermark = coding.datetime_from_wire(payload.get("watermark"))
    if not dry_run:
        db.commit()
    return report


def schema_format_version() -> int:
    from src.sync.payload import FORMAT_VERSION

    return FORMAT_VERSION


# --- tombstones -------------------------------------------------------------------------------


def _apply_tombstones(db: Session, tombstones: list[dict], report: MergeReport) -> None:
    """Delete what the peer deleted, and keep the tombstone so it relays onward."""
    from src.models import Tombstone

    for entry in tombstones:
        kind = entry.get("kind")
        ref = entry.get("ref")
        deleted_at = coding.datetime_from_wire(entry.get("deleted_at"))
        if not kind or not ref:
            continue

        existing = db.get(Tombstone, {"kind": kind, "ref": ref})
        if existing is None:
            # Keep the *originating* deleted_at, not now: the timestamp is what orders this against
            # edits, and rewriting it would make the same deletion resolve differently per device.
            db.add(Tombstone(kind=kind, ref=ref, deleted_at=deleted_at or datetime.utcnow()))
        elif deleted_at and existing.deleted_at and deleted_at < existing.deleted_at:
            # Keep the earliest: two devices deleting the same row should converge on one answer,
            # and "earliest" is computable by both without knowing who went first.
            existing.deleted_at = deleted_at

        spec = schema.BY_KIND.get(kind)
        if spec is None:
            # An unknown kind from a newer build. Stored above so it still relays, but nothing here
            # knows how to act on it.
            continue
        if _delete_local(db, spec, ref):
            report.deletions_applied += 1


def _local_tombstones(db: Session) -> dict[tuple[str, str], datetime | None]:
    from src.models import Tombstone

    return {
        (row.kind, row.ref): row.deleted_at
        for row in db.query(Tombstone).all()
    }


def _delete_local(db: Session, spec: schema.TableSpec, ref: str) -> bool:
    instance = _find_by_ref(db, spec, ref)
    if instance is None:
        return False
    db.delete(instance)
    return True


# --- records ----------------------------------------------------------------------------------


def _apply_record(
    db: Session,
    spec: schema.TableSpec,
    record: dict[str, Any],
    deleted: dict[tuple[str, str], datetime | None],
    report: MergeReport,
) -> None:
    ref = record.get("ref")
    if not ref:
        return
    incoming_at = coding.datetime_from_wire(record.get("updated_at"))

    tombstoned_at = deleted.get((spec.kind, ref), _MISSING)
    if tombstoned_at is not _MISSING:
        # Not strictly newer than the deletion -> the deletion wins. See the module docstring.
        if not (incoming_at and tombstoned_at and incoming_at > tombstoned_at):
            report._bump(report.skipped_deleted, spec.name)
            return

    if spec.behaviour == schema.OWNED:
        _apply_owned(db, spec, record, ref, incoming_at, report)
    elif spec.behaviour == schema.FACT:
        _apply_fact(db, spec, record, ref, incoming_at, report)
    else:
        _apply_link(db, spec, record, ref, incoming_at, report)


def _apply_fact(db, spec, record, ref, incoming_at, report) -> None:
    existing = _find_by_ref(db, spec, ref)
    if existing is None:
        instance = _construct(spec, record, include_immutable=True)
        if instance is None:
            report._bump(report.unresolved, spec.name)
            return
        db.add(instance)
        report._bump(report.inserted, spec.name)
        return

    # Present already: the money and the provider's own description are never rewritten by a peer.
    if _apply_mutable(spec, record, existing, incoming_at):
        report._bump(report.updated, spec.name)
    else:
        report._bump(report.skipped_older, spec.name)


def _apply_owned(db, spec, record, ref, incoming_at, report) -> None:
    model = schema.model_for(spec)
    incoming_uid = record.get("uid")

    existing = None
    if incoming_uid:
        existing = db.query(model).filter(model.uid == incoming_uid).first()

    if existing is None:
        by_name = _find_by_ref(db, spec, ref)
        if by_name is not None:
            existing = by_name
            # Same thing created independently on two devices. Converge on the lexicographically
            # smaller uid: arbitrary, but both devices compute the same answer with no coordination.
            local_uid = getattr(existing, "uid", None)
            if incoming_uid and (local_uid is None or incoming_uid < local_uid):
                if not db.query(model).filter(model.uid == incoming_uid).first():
                    existing.uid = incoming_uid
                    report.uid_converged += 1

    if existing is None:
        instance = _construct(spec, record, include_immutable=True)
        if instance is None:
            report._bump(report.unresolved, spec.name)
            return
        db.add(instance)
        report._bump(report.inserted, spec.name)
        return

    if _apply_mutable(spec, record, existing, incoming_at, report=report, db=db, spec_obj=spec):
        report._bump(report.updated, spec.name)
    else:
        report._bump(report.skipped_older, spec.name)


def _apply_link(db, spec, record, ref, incoming_at, report) -> None:
    existing = _find_by_ref(db, spec, ref)
    if existing is None:
        instance = _construct_link(db, spec, record, ref)
        if instance is None:
            # Parent not here yet -- deleted, or in a payload not merged yet. Payloads are full
            # state, so the next merge retries.
            report._bump(report.unresolved, spec.name)
            return
        db.add(instance)
        report._bump(report.inserted, spec.name)
        return

    if _apply_mutable(spec, record, existing, incoming_at):
        report._bump(report.updated, spec.name)
    else:
        report._bump(report.skipped_older, spec.name)


def _apply_mutable(
    spec: schema.TableSpec,
    record: dict[str, Any],
    existing,
    incoming_at: datetime | None,
    *,
    report: MergeReport | None = None,
    db: Session | None = None,
    spec_obj: schema.TableSpec | None = None,
) -> bool:
    """Whole-row last-write-wins over `spec.mutable`. True if anything changed.

    Ties are broken on **content**, not on locality. "Keep the local value on a tie" is the obvious
    rule and it silently breaks convergence: if two devices hold the same `updated_at` with different
    values, each keeps its own and they never agree, no matter how many times they sync. Exact ties
    are not hypothetical here either -- `backfill-sync-identity` deliberately assigns many rows the
    same derived timestamp, so a tie is the common case for pre-sync history.

    So on a tie both devices compare the same serialised values and pick the same winner. Arbitrary,
    but identical everywhere, which is the only property that matters.
    """
    if not spec.mutable:
        return False
    local_at = getattr(existing, "updated_at", None)
    if local_at is None:
        # An undated local row: normalise it to the same deterministic "unknown" the insert path uses,
        # so the two devices hold an identical value rather than NULL on one and the epoch on the
        # other. Behaviourally equivalent either way, but convergence is a property worth having
        # exactly -- and it cannot be assumed that a peer has run the backfill that removes NULLs.
        existing.updated_at = local_at = UNKNOWN_AGE
    if local_at is not None and incoming_at is not None:
        if incoming_at < local_at:
            return False
        if incoming_at == local_at and _content_key(spec, record) <= _local_content_key(
            spec, existing
        ):
            return False
    if incoming_at is None:
        # A peer with no opinion about age cannot overwrite a row we have a timestamp for.
        if local_at is not None:
            return False

    changed = False
    for field_spec in spec.mutable:
        if field_spec.name not in record:
            # A field this peer's build does not send. Absent is not the same as null: writing null
            # here would let an older build erase a column it cannot even see.
            continue
        value = field_spec.from_wire(record.get(field_spec.name))

        if field_spec.name == "name" and spec.behaviour == schema.OWNED:
            if not _rename_is_possible(db, spec_obj or spec, existing, value, report):
                continue

        if getattr(existing, field_spec.attribute, None) != value:
            setattr(existing, field_spec.attribute, value)
            changed = True

    if changed and incoming_at is not None:
        # Carry the peer's timestamp, not ours: otherwise every hop would look like a fresh edit and
        # the newest device would always win regardless of who actually edited last.
        existing.updated_at = incoming_at
    return changed


def _content_key(spec: schema.TableSpec, record: dict[str, Any]) -> str:
    """A deterministic ordering key for an incoming row's mutable values.

    JSON with sorted keys and fixed separators, so the same values always produce the same string on
    every device and every platform.
    """
    import json

    return json.dumps(
        {f.name: record.get(f.name) for f in spec.mutable}, sort_keys=True, default=str
    )


def _local_content_key(spec: schema.TableSpec, existing) -> str:
    """The same key for the local row, built through the same wire encoders.

    Encoding both sides identically is the point: comparing a `Decimal` against the string `"21.25"`
    would order by type rather than by value and give the two devices different answers.
    """
    import json

    return json.dumps(
        {f.name: f.to_wire(getattr(existing, f.attribute, None)) for f in spec.mutable},
        sort_keys=True,
        default=str,
    )


def _rename_is_possible(db, spec, existing, new_name, report) -> bool:
    """A rename must not collide with a different local row of that name.

    `projects.name` and `contacts.name` are unique, so applying the rename would raise and abort the
    whole merge -- one conflicting rename would stop this device syncing anything.
    """
    if db is None or not new_name or new_name == getattr(existing, "name", None):
        return True
    model = schema.model_for(spec)
    clash = db.query(model).filter(model.name == new_name).first()
    if clash is not None and clash is not existing:
        if report is not None:
            report.rename_conflicts += 1
        logger.info(
            "sync: not renaming %s to an existing name; the two rows stay separate", spec.name
        )
        return False
    return True


# --- lookup and construction -------------------------------------------------------------------

_MISSING = object()


def _find_by_ref(db: Session, spec: schema.TableSpec, ref: str):
    """Find the local row a reference names, or None."""
    model = schema.model_for(spec)
    try:
        parts = refs.decode(ref)
    except (ValueError, TypeError):
        return None

    if spec.kind == refs.KIND_TRANSACTION:
        source, source_id = parts[0], parts[1]
        return (
            db.query(model)
            .filter(model.source == source, model.source_id == source_id)
            .first()
        )
    if spec.kind in (refs.KIND_PROJECT, refs.KIND_CONTACT):
        return db.query(model).filter(model.name == parts[0]).first()
    if spec.kind in (refs.KIND_RULE, refs.KIND_SUBSCRIPTION):
        return db.query(model).filter(model.uid == parts[0]).first()

    ids = _resolve_link_ids(db, spec, parts)
    if ids is None:
        return None
    return db.query(model).filter_by(**ids).first()


def _resolve_link_ids(db: Session, spec: schema.TableSpec, parts: list) -> dict | None:
    """Turn a link's natural reference into local foreign keys, or None if a parent is absent."""
    from src.models import Contact, Project, Transaction

    def transaction_id(txn_ref: str) -> str | None:
        try:
            source, source_id = refs.decode(txn_ref)[:2]
        except (ValueError, TypeError):
            return None
        row = (
            db.query(Transaction.id)
            .filter(Transaction.source == source, Transaction.source_id == source_id)
            .first()
        )
        return row[0] if row else None

    def project_id(name: str) -> str | None:
        row = db.query(Project.id).filter(Project.name == name).first()
        return row[0] if row else None

    def contact_id(name: str) -> str | None:
        row = db.query(Contact.id).filter(Contact.name == name).first()
        return row[0] if row else None

    if spec.kind == refs.KIND_TRANSACTION_PROJECT:
        txn, project = transaction_id(parts[0]), project_id(parts[1])
        return None if not (txn and project) else {"transaction_id": txn, "project_id": project}

    if spec.kind == refs.KIND_PROJECT_MEMBER:
        project, contact = project_id(parts[0]), contact_id(parts[1])
        return None if not (project and contact) else {"project_id": project, "contact_id": contact}

    if spec.kind == refs.KIND_TRANSACTION_SPLIT:
        txn, contact = transaction_id(parts[0]), contact_id(parts[1])
        return None if not (txn and contact) else {"transaction_id": txn, "contact_id": contact}

    if spec.kind == refs.KIND_TRANSACTION_PROJECT_SPLIT:
        txn = transaction_id(parts[0])
        project, contact = project_id(parts[1]), contact_id(parts[2])
        if not (txn and project and contact):
            return None
        return {"transaction_id": txn, "project_id": project, "contact_id": contact}

    if spec.kind == refs.KIND_CONTACT_SPLITWISE_LINK:
        contact = contact_id(parts[0])
        return None if not contact else {"contact_id": contact}

    return None


def _construct(spec: schema.TableSpec, record: dict[str, Any], *, include_immutable: bool):
    """A new instance from a record. Local `id` is minted here -- it is per-device and never travels."""
    model = schema.model_for(spec)
    instance = model()
    if hasattr(model, "id"):
        instance.id = str(uuid.uuid4())
    fields = spec.all_fields if include_immutable else spec.mutable
    for field_spec in fields:
        if field_spec.name in record:
            setattr(instance, field_spec.attribute, field_spec.from_wire(record[field_spec.name]))
    _stamp(instance, record)
    return instance


def _construct_link(db: Session, spec: schema.TableSpec, record: dict[str, Any], ref: str):
    try:
        parts = refs.decode(ref)
    except (ValueError, TypeError):
        return None
    ids = _resolve_link_ids(db, spec, parts)
    if ids is None:
        return None
    model = schema.model_for(spec)
    instance = model(**ids)
    for field_spec in spec.mutable:
        if field_spec.name in record:
            setattr(instance, field_spec.attribute, field_spec.from_wire(record[field_spec.name]))
    _stamp(instance, record)
    return instance


#: "Unknown, and older than any real edit." Deterministic, so two devices inserting the same
#: undated row agree on its age.
UNKNOWN_AGE = datetime(1970, 1, 1)


def _stamp(instance, record: dict[str, Any]) -> None:
    """Set `updated_at` on a newly constructed row -- explicitly, including when it is unknown.

    Leaving it unset is the trap: the models declare `default=datetime.utcnow`, so an inserted row
    with no incoming timestamp is stamped **now**. That silently claims the row was edited this
    second, which then wins every future last-write-wins comparison against a peer holding the real,
    older value -- so a genuine annotation elsewhere gets overwritten by a stale one.

    Found on real data: 2427 transactions had a NULL `updated_at`, and merging them fabricated a
    fresh timestamp for every one.
    """
    incoming_at = coding.datetime_from_wire(record.get("updated_at"))
    instance.updated_at = incoming_at if incoming_at is not None else UNKNOWN_AGE
