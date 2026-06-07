from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re

from sqlalchemy.orm import Session

from src.models import Transaction


STOPWORDS = {
    "a",
    "an",
    "and",
    "card",
    "ca",
    "co",
    "com",
    "fre",
    "inc",
    "market",
    "mobile",
    "payment",
    "thank",
    "the",
    "you",
}


@dataclass(frozen=True)
class TxnView:
    id: str
    source_id: str
    source: str
    origin: str
    date: date
    amount: float
    merchant_raw: str


def audit_source_overlaps(db: Session, source: str, window_days: int = 3, rate_map: dict[str, float] | None = None) -> dict:
    rows = db.query(Transaction).filter(Transaction.source == source).order_by(Transaction.date, Transaction.id).all()

    # Determine the source currency for output conversion
    source_currency = "USD"
    if rows:
        source_currency = getattr(rows[0], "currency", None) or "USD"
    output_rate = (rate_map or {}).get(source_currency, 1.0)

    txns = [
        TxnView(
            id=row.id,
            source_id=row.source_id,
            source=row.source,
            origin=row.origin or "unknown",
            date=row.date,
            amount=float(row._amount),
            merchant_raw=row.merchant_raw,
        )
        for row in rows
    ]

    statement_txns = [txn for txn in txns if txn.origin == "statements"]
    plaid_txns = [txn for txn in txns if txn.origin == "plaid"]
    latest_statement_date = max((txn.date for txn in statement_txns), default=None)

    cross_source_matches = _match_cross_origin(statement_txns, plaid_txns, window_days)
    plaid_duplicate_matches = _match_plaid_duplicates(plaid_txns, window_days)

    def _conv(value: float) -> float:
        return round(value * output_rate, 2)

    return {
        "source": source,
        "db_balance": _conv(sum(txn.amount for txn in txns)),
        "statement_balance": _conv(sum(txn.amount for txn in statement_txns)),
        "plaid_balance_component": _conv(sum(txn.amount for txn in plaid_txns)),
        "latest_statement_date": latest_statement_date.isoformat() if latest_statement_date else None,
        "post_statement_plaid_total": _conv(
            sum(
                txn.amount
                for txn in plaid_txns
                if latest_statement_date and txn.date > latest_statement_date
            ),
        ) if latest_statement_date else None,
        "cross_source_overlap_total": _conv(sum(abs(match["amount"]) for match in cross_source_matches)),
        "cross_source_overlaps": cross_source_matches,
        "plaid_duplicate_total": _conv(sum(abs(match["amount"]) for match in plaid_duplicate_matches)),
        "plaid_duplicates": plaid_duplicate_matches,
    }


def audit_all_sources(db: Session, sources: list[str] | None = None, window_days: int = 3, rate_map: dict[str, float] | None = None) -> list[dict]:
    if not sources:
        rows = db.query(Transaction.source).distinct().order_by(Transaction.source).all()
        sources = [row[0] for row in rows]
    return [audit_source_overlaps(db, source, window_days=window_days, rate_map=rate_map) for source in sources]


def _match_cross_origin(statement_txns: list[TxnView], plaid_txns: list[TxnView], window_days: int) -> list[dict]:
    used_statement_ids: set[str] = set()
    used_plaid_ids: set[str] = set()
    matches: list[dict] = []

    candidates = []
    for statement in statement_txns:
        for plaid in plaid_txns:
            if not _amounts_match(statement.amount, plaid.amount):
                continue
            date_diff = abs((statement.date - plaid.date).days)
            if date_diff > window_days:
                continue
            if not _merchant_matches(statement.merchant_raw, plaid.merchant_raw):
                continue
            candidates.append((date_diff, abs(statement.amount), statement, plaid))

    for _, _, statement, plaid in sorted(candidates, key=lambda item: (item[0], item[1], item[2].date, item[3].date)):
        if statement.id in used_statement_ids or plaid.id in used_plaid_ids:
            continue
        used_statement_ids.add(statement.id)
        used_plaid_ids.add(plaid.id)
        matches.append(_format_match(statement, plaid))

    return matches


def _match_plaid_duplicates(plaid_txns: list[TxnView], window_days: int) -> list[dict]:
    used_ids: set[str] = set()
    matches: list[dict] = []
    candidates = []

    for idx, left in enumerate(plaid_txns):
        for right in plaid_txns[idx + 1 :]:
            if left.amount >= 0 and right.amount >= 0:
                continue
            if not _amounts_match(left.amount, right.amount):
                continue
            date_diff = abs((left.date - right.date).days)
            if date_diff > window_days:
                continue
            if not _merchant_matches(left.merchant_raw, right.merchant_raw):
                continue
            candidates.append((date_diff, abs(left.amount), left, right))

    for _, _, left, right in sorted(candidates, key=lambda item: (item[0], item[1], item[2].date, item[3].date)):
        if left.id in used_ids or right.id in used_ids:
            continue
        used_ids.add(left.id)
        used_ids.add(right.id)
        matches.append(_format_match(left, right))

    return matches


def _format_match(left: TxnView, right: TxnView) -> dict:
    return {
        "amount": round(left.amount, 2),
        "date_diff_days": abs((left.date - right.date).days),
        "left": {
            "id": left.id,
            "source_id": left.source_id,
            "origin": left.origin,
            "date": left.date.isoformat(),
            "amount": round(left.amount, 2),
            "merchant_raw": left.merchant_raw,
        },
        "right": {
            "id": right.id,
            "source_id": right.source_id,
            "origin": right.origin,
            "date": right.date.isoformat(),
            "amount": round(right.amount, 2),
            "merchant_raw": right.merchant_raw,
        },
    }


def _amounts_match(left: float, right: float) -> bool:
    return round(abs(left), 2) == round(abs(right), 2)


def _merchant_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False

    left_compact = _compact(left)
    right_compact = _compact(right)
    if left_compact == right_compact:
        return True
    if left_compact in right_compact or right_compact in left_compact:
        return True

    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    shared = left_tokens & right_tokens
    if shared:
        return True
    return False


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _tokens(value: str) -> list[str]:
    compacted = re.sub(r"[^a-z0-9]+", " ", value.lower()).split()
    return [token for token in compacted if len(token) > 2 and token not in STOPWORDS]
