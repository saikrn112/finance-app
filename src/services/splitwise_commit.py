"""Push a project's transactions to Splitwise as group expenses.

Splitwise creates one expense per API call, so a project with many transactions is drained
in batches: each press of Commit walks the pending queue up to `commit_batch_size` and
leaves the rest staged. `SplitwiseCommit` is that queue, so progress survives a crash, a
rate limit, or the user closing the tab.

Everything money-related comes from the same per-person figures the UI shows
(`_member_totals` in src/api/routes/projects.py builds them from the same split rows), so
what gets pushed can't drift from what was reviewed.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import logging
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.config import settings
from src.integrations import splitwise
from src.models import (
    Contact,
    ContactSplitwiseLink,
    Project,
    ProjectMember,
    SplitwiseCommit,
    Transaction,
    TransactionProject,
    TransactionSplit,
)

logger = logging.getLogger(__name__)


class SplitwiseNotReady(HTTPException):
    """Raised when a commit cannot proceed and the user must act first."""

    def __init__(self, detail: str, *, unmapped: list[str] | None = None):
        super().__init__(status_code=409, detail=detail)
        self.unmapped = unmapped or []


def self_contact(db: Session) -> Contact | None:
    return db.query(Contact).filter(Contact.is_self.is_(True)).first()


def contact_links(db: Session) -> dict[str, str]:
    """contact_id -> splitwise_user_id for every linked contact."""
    return {
        row.contact_id: row.splitwise_user_id
        for row in db.query(ContactSplitwiseLink).all()
    }


def unmapped_members(db: Session, project_id: str) -> list[Contact]:
    """Project members with no Splitwise link, in name order."""
    links = contact_links(db)
    members = (
        db.query(Contact)
        .join(ProjectMember, ProjectMember.contact_id == Contact.id)
        .filter(ProjectMember.project_id == project_id)
        .order_by(Contact.name)
        .all()
    )
    return [m for m in members if m.id not in links]


def _share_fingerprint(shares: dict[str, Decimal], cost: Decimal, description: str) -> str:
    """Detects a post-commit edit, so an amend is only offered when something changed."""
    payload = "|".join(
        [f"{cost:.2f}", description or ""]
        + [f"{cid}:{amount:.2f}" for cid, amount in sorted(shares.items())]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _owed_shares(db: Session, txn: Transaction, project_id: str) -> dict[str, Decimal]:
    """Per-contact owed amounts in the transaction's own currency.

    Explicit `share_amount` values win; otherwise the transaction divides equally, which is
    the default everywhere else in the app.
    """
    # Shares come from the transaction. The Splitwise *group* is still per project, so a
    # transaction in two projects still pushes one expense per group — unchanged behaviour.
    rows = db.query(TransactionSplit).filter_by(transaction_id=txn.id).all()
    if not rows:
        return {}
    total = abs(Decimal(str(txn._amount or 0)))
    explicit = {r.contact_id: r._share_amount for r in rows if r._share_amount is not None}
    if explicit and len(explicit) == len(rows):
        return {cid: Decimal(str(v)).quantize(Decimal("0.01")) for cid, v in explicit.items()}
    each = (total / len(rows)).quantize(Decimal("0.01"))
    shares = {r.contact_id: each for r in rows}
    # Put any rounding remainder on a stable participant so the total lands exactly.
    drift = total - sum(shares.values())
    if drift and shares:
        first = sorted(shares)[0]
        shares[first] = (shares[first] + drift).quantize(Decimal("0.01"))
    return shares


def pending_summary(db: Session, project_id: str) -> dict[str, Any]:
    """What a commit would do, without doing it."""
    linked = db.query(TransactionProject).filter_by(project_id=project_id).count()
    commits = {
        (c.transaction_id): c
        for c in db.query(SplitwiseCommit).filter_by(project_id=project_id).all()
    }
    committed = sum(1 for c in commits.values() if c.state == "committed")
    failed = [
        {"transaction_id": c.transaction_id, "error": c.error}
        for c in commits.values() if c.state == "failed"
    ]
    return {
        "project_id": project_id,
        "transactions": linked,
        "committed": committed,
        "pending": max(0, linked - committed),
        "failed": failed,
        "batch_size": settings.splitwise.commit_batch_size,
        "unmapped_members": [m.name for m in unmapped_members(db, project_id)],
    }


def ensure_group(
    db: Session,
    project: Project,
    access_token: str,
    links: dict[str, str],
    api: Any = splitwise,
) -> str:
    """Reuse the cached Splitwise group for this project, creating it on first commit."""
    if project.splitwise_group_id:
        return project.splitwise_group_id
    member_ids = [int(links[m.id]) for m in _project_members(db, project.id) if m.id in links]
    group = api.create_group(
        access_token,
        name=project.name,
        member_user_ids=member_ids,
    )
    project.splitwise_group_id = str(group["id"])
    db.commit()
    return project.splitwise_group_id


def _project_members(db: Session, project_id: str) -> list[Contact]:
    return (
        db.query(Contact)
        .join(ProjectMember, ProjectMember.contact_id == Contact.id)
        .filter(ProjectMember.project_id == project_id)
        .order_by(Contact.name)
        .all()
    )


def commit_project(
    db: Session,
    project_id: str,
    access_token: str,
    *,
    batch_size: int | None = None,
    amend: bool = False,
    api: Any = splitwise,
    now: Callable[[], datetime] = datetime.utcnow,
) -> dict[str, Any]:
    """Commit up to `batch_size` transactions. Call again to drain the rest.

    `api` is injectable so the batching and staging logic can be tested without touching
    the network.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    me = self_contact(db)
    if not me:
        raise SplitwiseNotReady(
            "Mark one person as yourself in Settings first — Splitwise needs to know who paid."
        )
    links = contact_links(db)
    if me.id not in links:
        raise SplitwiseNotReady(f"Link {me.name} to a Splitwise account first.", unmapped=[me.name])

    missing = unmapped_members(db, project_id)
    if missing:
        # Refuse rather than silently misreporting what someone owes.
        names = ", ".join(m.name for m in missing)
        raise SplitwiseNotReady(
            f"Link these people to Splitwise before committing: {names}",
            unmapped=[m.name for m in missing],
        )

    group_id = ensure_group(db, project, access_token, links, api=api)

    txns = (
        db.query(Transaction)
        .join(TransactionProject, Transaction.id == TransactionProject.transaction_id)
        .filter(TransactionProject.project_id == project_id)
        .order_by(Transaction.date)
        .all()
    )
    descriptions = {
        row.transaction_id: row.description
        for row in db.query(TransactionProject).filter_by(project_id=project_id).all()
    }
    existing = {
        c.transaction_id: c
        for c in db.query(SplitwiseCommit).filter_by(project_id=project_id).all()
    }

    limit = batch_size or settings.splitwise.commit_batch_size
    created, updated, skipped, failed = 0, 0, 0, []

    for txn in txns:
        if created + updated >= limit:
            break

        shares = _owed_shares(db, txn, project_id)
        if not shares:
            skipped += 1
            continue

        cost = abs(Decimal(str(txn._amount or 0)))
        if cost == 0:
            skipped += 1
            continue

        description = descriptions.get(txn.id) or txn.merchant_clean or txn.merchant_raw or "Expense"
        fingerprint = _share_fingerprint(shares, cost, description)
        record = existing.get(txn.id)

        if record and record.state == "committed":
            unchanged = record.committed_fingerprint == fingerprint
            if unchanged or not amend:
                skipped += 1
                continue

        owed_by_user = {int(links[cid]): amount for cid, amount in shares.items() if cid in links}
        if len(owed_by_user) != len(shares):
            # Should be unreachable — unmapped members are rejected above — but never guess
            # at someone's share.
            skipped += 1
            continue

        payload = api.build_expense_payload(
            cost=cost,
            description=description,
            group_id=int(group_id) if group_id else None,
            payer_user_id=int(links[me.id]),
            owed_shares=owed_by_user,
            expense_date=txn.date,
            currency_code=(txn.currency or "USD"),
        )

        if record is None:
            record = SplitwiseCommit(transaction_id=txn.id, project_id=project_id, state="pending")
            db.add(record)
        record.attempted_at = now()

        try:
            if record.state == "committed" and record.expense_id and amend:
                api.update_expense(access_token, int(record.expense_id), payload)
                updated += 1
            else:
                expense = api.create_expense(access_token, payload)
                record.expense_id = str(expense.get("id") or "")
                created += 1
            record.state = "committed"
            record.committed_at = now()
            record.committed_fingerprint = fingerprint
            record.error = None
        except HTTPException as exc:
            record.state = "failed"
            record.error = str(exc.detail)[:500]
            failed.append({"transaction_id": txn.id, "error": record.error})
            logger.warning("Splitwise commit failed for %s: %s", txn.id, record.error)
            # Persist progress so far and stop: a 401 or rate limit will only repeat.
            db.commit()
            break
        # Commit per expense so a later failure cannot undo earlier successes.
        db.commit()

    db.commit()
    summary = pending_summary(db, project_id)
    summary.update({
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "batch_failures": failed,
        "group_id": group_id,
        "done": summary["pending"] == 0 and not failed,
    })
    return summary
