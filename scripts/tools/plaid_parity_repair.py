#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.ingestion.plaid_client import get_plaid_client
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.exceptions import ApiException

from src.models import SessionLocal, SyncLog, Transaction, init_db
from src.ingestion.plaid_sync import upsert_plaid_transaction, remove_plaid_transaction
from src.processing.categorizer import RuleMatcher


@dataclass
class CurrentTxn:
    institution: str
    source_id: str
    account_id: str | None
    mask: str | None
    authorized_date: date | None
    posted_date: date
    amount: float
    merchant_raw: str
    merchant_clean: str
    pending: bool
    pending_transaction_id: str | None
    original_description: str | None
    payment_channel: str | None
    raw: dict[str, Any]


def _connected_logs(db) -> list[SyncLog]:
    return (
        db.query(SyncLog)
        .filter(SyncLog.source == "plaid", SyncLog.status == "connected")
        .order_by(SyncLog.created_at.asc())
        .all()
    )


def _transform_full_txn(institution: str, t, account_map: dict[str, Any]) -> CurrentTxn:
    account = account_map.get(getattr(t, "account_id", None))
    authorized = getattr(t, "authorized_date", None)
    auth_date = authorized if isinstance(authorized, date) else (date.fromisoformat(str(authorized)) if authorized else None)
    posted = t.date if isinstance(t.date, date) else date.fromisoformat(str(t.date))
    payment_channel = getattr(t, "payment_channel", None)
    payment_channel_value = getattr(payment_channel, "value", payment_channel)
    raw = {
        "source_id": t.transaction_id,
        "date": posted,
        "authorized_date": auth_date,
        "amount": -float(t.amount),
        "merchant_raw": t.name,
        "merchant_clean": t.merchant_name or t.name,
        "account_last4": getattr(account, "mask", None) or (t.account_id[-4:] if getattr(t, "account_id", None) else None),
        "plaid_account_id": getattr(t, "account_id", None),
        "plaid_mask": getattr(account, "mask", None),
        "original_description": getattr(t, "original_description", None),
        "payment_channel": payment_channel_value,
        "plaid_category": t.personal_finance_category.primary if getattr(t, "personal_finance_category", None) else None,
        "pending_transaction_id": getattr(t, "pending_transaction_id", None),
        "pending": getattr(t, "pending", False),
    }
    return CurrentTxn(
        institution=institution,
        source_id=t.transaction_id,
        account_id=getattr(t, "account_id", None),
        mask=getattr(account, "mask", None),
        authorized_date=auth_date,
        posted_date=posted,
        amount=-float(t.amount),
        merchant_raw=t.name,
        merchant_clean=t.merchant_name or t.name,
        pending=bool(getattr(t, "pending", False)),
        pending_transaction_id=getattr(t, "pending_transaction_id", None),
        original_description=getattr(t, "original_description", None),
        payment_channel=payment_channel_value,
        raw=raw,
    )


def fetch_current_plaid_state(access_token: str, institution: str) -> tuple[dict[str, CurrentTxn], list[dict[str, Any]]]:
    client = get_plaid_client()
    cursor = ""
    has_more = True
    current: dict[str, CurrentTxn] = {}
    removed_events: list[dict[str, Any]] = []
    while has_more:
        response = client.transactions_sync(
            TransactionsSyncRequest(
                access_token=access_token,
                cursor=cursor,
                options={"include_original_description": True},
            )
        )
        account_map = {a.account_id: a for a in getattr(response, "accounts", []) or []}
        for txn in getattr(response, "added", []) or []:
            transformed = _transform_full_txn(institution, txn, account_map)
            current[transformed.source_id] = transformed
        for txn in getattr(response, "modified", []) or []:
            transformed = _transform_full_txn(institution, txn, account_map)
            current[transformed.source_id] = transformed
        for removed in getattr(response, "removed", []) or []:
            txn_id = getattr(removed, "transaction_id", None)
            removed_events.append(
                {
                    "transaction_id": txn_id,
                    "account_id": getattr(removed, "account_id", None),
                }
            )
            if txn_id:
                current.pop(txn_id, None)
        cursor = response.next_cursor
        has_more = response.has_more
    return current, removed_events


def _local_rows(db, institution: str) -> list[Transaction]:
    return (
        db.query(Transaction)
        .filter(Transaction.origin == "plaid", Transaction.source == institution)
        .all()
    )


def audit_institution(db, log: SyncLog) -> dict[str, Any]:
    institution = (log.extra_data or {}).get("institution_name", "Unknown")
    access_token = (log.extra_data or {}).get("access_token")
    try:
        current_state, removed_events = fetch_current_plaid_state(access_token, institution)
    except ApiException as exc:
        return {
            "institution": institution,
            "error": {
                "status": getattr(exc, "status", None),
                "reason": getattr(exc, "reason", ""),
                "body": getattr(exc, "body", ""),
            },
        }
    local_rows = _local_rows(db, institution)
    local_by_source = {row.source_id: row for row in local_rows}

    missing_in_db = [txn for source_id, txn in current_state.items() if source_id not in local_by_source]
    extra_in_db = [row for row in local_rows if row.source_id not in current_state]
    mismatched_pending = []
    for source_id, txn in current_state.items():
        row = local_by_source.get(source_id)
        if not row:
            continue
        if bool(row.pending) != txn.pending:
            mismatched_pending.append(
                {
                    "source_id": source_id,
                    "db_pending": bool(row.pending),
                    "plaid_pending": txn.pending,
                    "merchant": txn.merchant_raw,
                    "amount": txn.amount,
                }
            )

    by_mask = Counter((txn.mask or "unknown") for txn in current_state.values())
    return {
        "institution": institution,
        "plaid_current_count": len(current_state),
        "local_count": len(local_rows),
        "missing_in_db": missing_in_db,
        "extra_in_db": extra_in_db,
        "mismatched_pending": mismatched_pending,
        "removed_events": removed_events,
        "by_mask": by_mask,
    }


def repair_institution(db, log: SyncLog, remove_extras: bool = False) -> dict[str, Any]:
    institution = (log.extra_data or {}).get("institution_name", "Unknown")
    access_token = (log.extra_data or {}).get("access_token")
    try:
        current_state, _ = fetch_current_plaid_state(access_token, institution)
    except ApiException as exc:
        return {
            "institution": institution,
            "error": {
                "status": getattr(exc, "status", None),
                "reason": getattr(exc, "reason", ""),
                "body": getattr(exc, "body", ""),
            },
        }
    local_rows = _local_rows(db, institution)
    local_by_source = {row.source_id: row for row in local_rows}
    matcher = RuleMatcher()

    added = 0
    updated = 0
    promoted = 0
    removed = 0

    for txn in sorted(current_state.values(), key=lambda item: (item.authorized_date or item.posted_date, item.posted_date, item.source_id)):
        action = upsert_plaid_transaction(db, institution, txn.raw, matcher)
        if action == "added":
            added += 1
        elif action == "pending_promoted":
            promoted += 1
        else:
            updated += 1

    if remove_extras:
        for row in local_rows:
            if row.source_id not in current_state:
                removed += remove_plaid_transaction(db, institution, row.source_id)

    db.commit()
    return {
        "institution": institution,
        "added": added,
        "updated": updated,
        "pending_promotions": promoted,
        "removed": removed,
    }


def _serialize_missing(txn: CurrentTxn) -> dict[str, Any]:
    return {
        "source_id": txn.source_id,
        "effective_date": (txn.authorized_date or txn.posted_date).isoformat(),
        "posted_date": txn.posted_date.isoformat(),
        "amount": txn.amount,
        "merchant_raw": txn.merchant_raw,
        "merchant_clean": txn.merchant_clean,
        "mask": txn.mask,
        "pending": txn.pending,
        "pending_transaction_id": txn.pending_transaction_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and repair Plaid-vs-DB transaction parity.")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit")
    audit.add_argument("--institution", help="Institution name filter")
    audit.add_argument("--show-missing", type=int, default=10)

    repair = sub.add_parser("repair")
    repair.add_argument("--institution", help="Institution name filter")
    repair.add_argument("--remove-extras", action="store_true")

    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        logs = _connected_logs(db)
        if args.institution:
            logs = [log for log in logs if (log.extra_data or {}).get("institution_name") == args.institution]

        if args.command == "audit":
            payload = []
            for log in logs:
                result = audit_institution(db, log)
                payload.append(
                    {
                        "institution": result["institution"],
                        **(
                            {"error": result["error"]}
                            if "error" in result
                            else {
                                "plaid_current_count": result["plaid_current_count"],
                                "local_count": result["local_count"],
                                "missing_count": len(result["missing_in_db"]),
                                "extra_count": len(result["extra_in_db"]),
                                "mismatched_pending_count": len(result["mismatched_pending"]),
                                "by_mask": dict(result["by_mask"]),
                                "missing_examples": [_serialize_missing(txn) for txn in result["missing_in_db"][: args.show_missing]],
                            }
                        ),
                    }
                )
            print(json.dumps(payload, indent=2))
            return

        repaired = [repair_institution(db, log, remove_extras=args.remove_extras) for log in logs]
        print(json.dumps(repaired, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
