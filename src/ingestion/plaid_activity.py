from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.models import AccountActivity, Transaction, TransactionProject
from src.plugins.registry import classify_source, get_all_sources


def plaid_transactions_destination(source: str) -> str:
    plugin = get_all_sources().get(classify_source(source))
    configured = getattr(plugin, "plaid_transactions_destination", None) if plugin else None
    if configured in {"transactions", "account_activity"}:
        return configured
    return "account_activity" if plugin and plugin.domain in {"investments", "retirement"} else "transactions"


@dataclass
class AccountActivitySyncCounts:
    added: int = 0
    updated: int = 0
    removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"added": self.added, "updated": self.updated, "removed": self.removed}


def _activity_type(data: dict[str, Any]) -> str:
    if data.get("activity_type"):
        return str(data["activity_type"])
    text = " ".join(
        str(data.get(key) or "")
        for key in ("merchant_clean", "merchant_raw", "original_description", "plaid_category")
    ).casefold()
    if "interest" in text:
        return "interest"
    if any(token in text for token in ("transfer", "deposit", "withdrawal", "ach")):
        return "transfer"
    if any(token in text for token in ("dividend", "distribution")):
        return "dividend"
    if any(token in text for token in ("buy", "sell", "trade")):
        return "trade"
    return "other"


def apply_account_activity_batch(
    db: Session,
    *,
    institution: str,
    added: list[dict[str, Any]],
    modified: list[dict[str, Any]],
    removed: list[str],
) -> AccountActivitySyncCounts:
    counts = AccountActivitySyncCounts()
    for data in [*added, *modified]:
        row = (
            db.query(AccountActivity)
            .filter(AccountActivity.source == institution, AccountActivity.source_id == data["source_id"])
            .first()
        )
        is_new = row is None
        if row is None:
            row = AccountActivity(source=institution, source_id=data["source_id"])
            db.add(row)
        row.source_key = classify_source(institution) or institution
        row.account_id = data.get("plaid_account_id")
        row.account_last4 = data.get("account_last4")
        row.date = data["date"]
        row.authorized_date = data.get("authorized_date")
        row.amount = data["amount"]
        row.description = data.get("original_description") or data.get("merchant_raw") or "Activity"
        row.merchant = data.get("merchant_clean") or data.get("merchant_raw")
        row.activity_type = _activity_type(data)
        row.currency = data.get("currency") or "USD"
        row.pending = bool(data.get("pending"))
        row.pending_activity_id = data.get("pending_transaction_id")
        row.raw_data = {
            "payment_channel": data.get("payment_channel"),
            "plaid_category": data.get("plaid_category"),
            **(data.get("raw_data") or {}),
        }
        if is_new:
            counts.added += 1
        else:
            counts.updated += 1

    if removed:
        deleted = (
            db.query(AccountActivity)
            .filter(AccountActivity.source == institution, AccountActivity.source_id.in_(removed))
            .delete(synchronize_session=False)
        )
        counts.removed += int(deleted or 0)
    return counts


def migrate_investment_transactions(db: Session) -> int:
    """One-time idempotent migration for Plaid rows stored in the household ledger."""
    investment_sources = {
        alias
        for plugin in get_all_sources().values()
        if plaid_transactions_destination(plugin.label) == "account_activity"
        for alias in {plugin.label, *plugin.source_aliases}
    }
    if not investment_sources:
        return 0
    rows = (
        db.query(Transaction)
        .filter(Transaction.origin == "plaid", Transaction.source.in_(investment_sources))
        .all()
    )
    moved = 0
    for txn in rows:
        data = {
            "source_id": txn.source_id,
            "date": txn.date,
            "authorized_date": txn.authorized_date,
            "amount": float(txn._amount or 0),
            "merchant_raw": txn.merchant_raw,
            "merchant_clean": txn.merchant_clean,
            "account_last4": txn.account_last4,
            "plaid_account_id": txn.plaid_account_id,
            "original_description": txn.original_description,
            "payment_channel": txn.payment_channel,
            "pending_transaction_id": txn.pending_transaction_id,
            "pending": txn.pending,
            "currency": txn.currency,
            "plaid_category": txn.category,
        }
        apply_account_activity_batch(db, institution=txn.source, added=[data], modified=[], removed=[])
        db.query(TransactionProject).filter(TransactionProject.transaction_id == txn.id).delete(synchronize_session=False)
        db.delete(txn)
        moved += 1
    if moved:
        db.commit()
    return moved


def reroute_account_activity_to_transactions(db: Session, *, apply: bool = False) -> dict[str, int]:
    """Move Plaid Transactions rows to the ledger when plugin policy opts in."""
    from src.ingestion.plaid_sync import apply_plaid_sync_batch
    from src.processing.categorizer import RuleMatcher

    ledger_sources = {
        alias
        for plugin in get_all_sources().values()
        if getattr(plugin, "plaid_transactions_destination", None) == "transactions"
        for alias in {plugin.label, *plugin.source_aliases}
    }
    rows = db.query(AccountActivity).filter(AccountActivity.source.in_(ledger_sources)).all() if ledger_sources else []
    if not apply:
        return {"candidates": len(rows), "moved": 0, "recategorized": 0}

    matcher = RuleMatcher()
    moved = 0
    for row in rows:
        raw = row.raw_data or {}
        data = {
            "source_id": row.source_id,
            "date": row.date,
            "authorized_date": row.authorized_date,
            "amount": float(row._amount or 0),
            "merchant_raw": row.description,
            "merchant_clean": row.merchant or row.description,
            "account_last4": row.account_last4,
            "plaid_account_id": row.account_id,
            "original_description": row.description,
            "payment_channel": raw.get("payment_channel"),
            "pending_transaction_id": row.pending_activity_id,
            "pending": row.pending,
            "currency": row.currency,
            "plaid_category": raw.get("plaid_category"),
        }
        apply_plaid_sync_batch(db, institution=row.source, added=[data], modified=[], removed=[], matcher=matcher)
        db.delete(row)
        moved += 1
    db.flush()
    recategorized = 0
    for txn in db.query(Transaction).filter(
        Transaction.origin == "plaid",
        Transaction.source.in_(ledger_sources),
        or_(Transaction.category_source.is_(None), Transaction.category_source != "user"),
    ).all():
        result = matcher.match(txn.merchant_raw)
        if result and (txn.category != result.category or (result.merchant_clean and txn.merchant_clean != result.merchant_clean)):
            txn.category = result.category
            txn.category_source = result.source
            if result.merchant_clean:
                txn.merchant_clean = result.merchant_clean
            recategorized += 1
    db.commit()
    return {"candidates": len(rows), "moved": moved, "recategorized": recategorized}
