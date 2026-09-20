from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from src.models import (
    Transaction,
    TransactionProject,
    TransactionProjectSplit,
    TransactionSplit,
)
from src.processing.categorizer import RuleMatcher
from src.processing.overlap_diagnostics import _merchant_matches


@dataclass
class PlaidSyncCounts:
    added: int = 0
    updated: int = 0
    removed: int = 0
    pending_promotions: int = 0
    skipped_pending: int = 0
    skipped_statement_overlap: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "pending_promotions": self.pending_promotions,
            "skipped_pending": self.skipped_pending,
            "skipped_statement_overlap": self.skipped_statement_overlap,
        }


def apply_plaid_sync_batch(
    db: Session,
    institution: str,
    added: list[dict[str, Any]],
    modified: list[dict[str, Any]],
    removed: list[str],
    matcher: RuleMatcher | None = None,
) -> PlaidSyncCounts:
    counts = PlaidSyncCounts()
    matcher = matcher or RuleMatcher()

    for txn_data in added:
        action = upsert_plaid_transaction(db, institution, txn_data, matcher)
        if action == "added":
            counts.added += 1
        elif action == "pending_promoted":
            counts.pending_promotions += 1
        elif action in {"skipped_pending", "skipped_pending_removed"}:
            counts.skipped_pending += 1
        elif action == "skipped_statement_overlap":
            counts.skipped_statement_overlap += 1
        else:
            counts.updated += 1

    for txn_data in modified:
        action = upsert_plaid_transaction(db, institution, txn_data, matcher)
        if action == "added":
            counts.added += 1
        elif action == "pending_promoted":
            counts.pending_promotions += 1
        elif action in {"skipped_pending", "skipped_pending_removed"}:
            counts.skipped_pending += 1
        elif action == "skipped_statement_overlap":
            counts.skipped_statement_overlap += 1
        else:
            counts.updated += 1

    for source_id in removed:
        counts.removed += remove_plaid_transaction(db, institution, source_id)

    return counts


def upsert_plaid_transaction(
    db: Session,
    institution: str,
    txn_data: dict[str, Any],
    matcher: RuleMatcher,
) -> str:
    statement_twin = _find_statement_boundary_overlap(db, institution, txn_data)
    if statement_twin:
        # The statement row already represents this expense, so the Plaid copy goes -- but anything
        # the user attached to it moves onto the survivor first. Deleting outright discarded real
        # work: a project assignment and a split nobody had a second copy of.
        doomed = _find_plaid_txn(db, institution, txn_data["source_id"])
        if doomed is not None:
            _transfer_user_attributes(db, doomed, statement_twin)
        remove_plaid_transaction(db, institution, txn_data["source_id"])
        return "skipped_statement_overlap"

    existing = _find_plaid_txn(db, institution, txn_data["source_id"])
    pending_id = txn_data.get("pending_transaction_id")
    pending_existing = _find_plaid_txn(db, institution, pending_id) if pending_id else None

    if existing and pending_existing and existing.id != pending_existing.id:
        _transfer_user_attributes(db, pending_existing, existing)
        db.delete(pending_existing)
        pending_existing = None

    if existing:
        _apply_txn_data(existing, txn_data, matcher)
        return "updated"

    if pending_existing:
        pending_existing.source_id = txn_data["source_id"]
        _apply_txn_data(pending_existing, txn_data, matcher)
        return "pending_promoted"

    replaced_item_txn = _find_replaced_item_txn(db, institution, txn_data)
    if replaced_item_txn:
        # Plaid IDs change when the same account is linked through a new Item.
        # Reuse the enriched row so user categories and projects survive.
        replaced_item_txn.source_id = txn_data["source_id"]
        _apply_txn_data(replaced_item_txn, txn_data, matcher)
        return "item_replaced"

    txn = Transaction(
        source=institution,
        source_id=txn_data["source_id"],
        origin="plaid",
    )
    _apply_txn_data(txn, txn_data, matcher)
    db.add(txn)
    return "added"


def remove_plaid_transaction(db: Session, institution: str, source_id: str) -> int:
    txn = _find_plaid_txn(db, institution, source_id)
    if not txn:
        return 0
    _delete_transaction_links(db, txn.id)
    db.delete(txn)
    return 1


def _delete_transaction_links(db: Session, transaction_id: str) -> int:
    """Remove the join rows that point at a transaction being deleted.

    SQLite declares these foreign keys `NO ACTION` and runs with `PRAGMA foreign_keys = 0`, so
    deleting a transaction silently leaves them dangling rather than refusing. Left behind they are
    invisible in the app (every read joins through the parent) *and* unsyncable, because a row that
    cannot be named cannot be put in a payload -- which is how 77 of them accumulated unnoticed.
    """
    removed = 0
    for model in (TransactionProject, TransactionSplit, TransactionProjectSplit):
        removed += (
            db.query(model).filter(model.transaction_id == transaction_id).delete(
                synchronize_session=False
            )
            or 0
        )
    db.flush()
    return removed


def _find_plaid_txn(db: Session, institution: str, source_id: str | None) -> Transaction | None:
    if not source_id:
        return None
    return db.query(Transaction).filter(
        Transaction.source == institution,
        Transaction.origin == "plaid",
        Transaction.source_id == source_id,
    ).first()


def _find_replaced_item_txn(
    db: Session,
    institution: str,
    txn_data: dict[str, Any],
) -> Transaction | None:
    account_id = txn_data.get("plaid_account_id")
    if not account_id:
        return None
    merchant = (txn_data.get("merchant_clean") or txn_data.get("merchant_raw") or "").strip().casefold()
    candidates = db.query(Transaction).filter(
        Transaction.source == institution,
        Transaction.origin == "plaid",
        Transaction.plaid_account_id.isnot(None),
        Transaction.plaid_account_id != account_id,
        Transaction.account_last4 == txn_data.get("account_last4"),
        Transaction.date == txn_data["date"],
        Transaction._amount == txn_data["amount"],
    ).order_by(Transaction.created_at.asc()).all()
    for candidate in candidates:
        candidate_merchant = (candidate.merchant_clean or candidate.merchant_raw or "").strip().casefold()
        if candidate_merchant == merchant:
            return candidate
    return None


def _transfer_user_attributes(db: Session, src: Transaction, dest: Transaction) -> None:
    if src.category_source == "user":
        dest.category = src.category
        dest.category_source = src.category_source
    if src.notes:
        dest.notes = src.notes
    if src.tags:
        dest.tags = src.tags
    project_links = db.query(TransactionProject).filter(
        TransactionProject.transaction_id == src.id
    ).all()
    for link in project_links:
        existing = db.query(TransactionProject).filter(
            TransactionProject.transaction_id == dest.id,
            TransactionProject.project_id == link.project_id,
        ).first()
        if not existing:
            db.add(
                TransactionProject(
                    transaction_id=dest.id,
                    project_id=link.project_id,
                    description=link.description,
                )
            )
        db.delete(link)

    # Splits move with the row too. They did not, which meant a pending charge that had been split
    # with someone kept its project when it posted but silently lost who owed what -- and because
    # SQLite is not enforcing these foreign keys, the split rows were left pointing at a deleted
    # transaction rather than raising.
    for model, key in ((TransactionSplit, ("contact_id",)),
                       (TransactionProjectSplit, ("project_id", "contact_id"))):
        for link in db.query(model).filter(model.transaction_id == src.id).all():
            match = db.query(model).filter(
                model.transaction_id == dest.id,
                *[getattr(model, name) == getattr(link, name) for name in key],
            ).first()
            if not match:
                fields = {name: getattr(link, name) for name in key}
                moved = model(transaction_id=dest.id, **fields)
                # Private column: reading the public property is deliberately blocked.
                moved._share_amount = link._share_amount
                db.add(moved)
            db.delete(link)
    db.flush()


def _apply_txn_data(txn: Transaction, txn_data: dict[str, Any], matcher: RuleMatcher) -> None:
    category = matcher.match(txn_data["merchant_raw"])
    if not category and txn_data.get("merchant_clean"):
        category = matcher.match(txn_data["merchant_clean"])

    txn.date = txn_data["date"]
    txn.authorized_date = txn_data.get("authorized_date")
    txn.amount = txn_data["amount"]
    txn.merchant_raw = txn_data["merchant_raw"]
    txn.merchant_clean = txn_data["merchant_clean"]
    txn.account_last4 = txn_data["account_last4"]
    txn.plaid_account_id = txn_data.get("plaid_account_id")
    txn.plaid_mask = txn_data.get("plaid_mask")
    txn.original_description = txn_data.get("original_description")
    txn.payment_channel = txn_data.get("payment_channel")
    txn.currency = txn_data.get("currency", "USD")
    txn.origin = "plaid"
    txn.pending = bool(txn_data.get("pending"))
    txn.pending_transaction_id = txn_data.get("pending_transaction_id")
    if txn.category_source != "user" and category:
        txn.category = category.category
        txn.category_source = "rule"
    elif not txn.category or txn.category == "Uncategorized":
        txn.category = "Uncategorized"
        txn.category_source = "default"


def _find_statement_boundary_overlap(db: Session, institution: str, txn_data: dict[str, Any]) -> Transaction | None:
    latest_statement = db.query(Transaction).filter(
        Transaction.source == institution,
        Transaction.origin == "statements",
    ).order_by(Transaction.date.desc()).first()
    if not latest_statement:
        return None

    txn_date = txn_data["date"]
    if txn_date > latest_statement.date + timedelta(days=1):
        return None

    nearby = db.query(Transaction).filter(
        Transaction.source == institution,
        Transaction.origin == "statements",
        Transaction.date >= txn_date - timedelta(days=3),
        Transaction.date <= txn_date + timedelta(days=3),
    ).all()
    for statement_txn in nearby:
        if round(abs(float(statement_txn._amount)), 2) != round(abs(float(txn_data["amount"])), 2):
            continue
        if _merchant_matches(statement_txn.merchant_raw, txn_data["merchant_raw"]):
            return statement_txn
    return None
