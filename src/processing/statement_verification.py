"""Read-only, account-scoped statement evidence comparison.

Amounts remain in the statement currency. This verifies supplied evidence; it never
repairs the ledger or treats a balanced period as proof that every row is present.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from src.models import Transaction


class StatementLine(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    date: date
    amount: Decimal
    description: str = Field(min_length=1)


class StatementEvidence(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    source: str = Field(min_length=1)
    account_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    plaid_account_id: str | None = None
    period_start: date
    period_end: date
    opening_balance: Decimal
    closing_balance: Decimal
    balance_convention: str = Field(default="asset", pattern=r"^(asset|debt)$")
    # None means only balance evidence was supplied. [] proves a zero-row statement.
    transactions: list[StatementLine] | None = None

    @model_validator(mode="after")
    def validate_period(self):
        if self.period_end < self.period_start:
            raise ValueError("Statement end must be on or after start")
        if self.transactions is not None and any(
            not self.period_start <= row.date <= self.period_end for row in self.transactions
        ):
            raise ValueError("All statement rows must fall within the inclusive period")
        return self


def verify_statement(db: Session, evidence: StatementEvidence, currency: str) -> dict:
    query = db.query(Transaction).filter(
        Transaction.source == evidence.source,
        Transaction.currency == currency,
        Transaction.date >= evidence.period_start,
        Transaction.date <= evidence.period_end,
    )
    scoped = bool(evidence.plaid_account_id or evidence.account_last4)
    if evidence.plaid_account_id:
        query = query.filter(Transaction.plaid_account_id == evidence.plaid_account_id)
    if evidence.account_last4:
        query = query.filter(
            (Transaction.account_last4 == evidence.account_last4)
            | (Transaction.plaid_mask == evidence.account_last4)
        )
    rows = query.order_by(Transaction.date, Transaction.id).all()
    posted = [row for row in rows if not row.pending]
    account_keys = {
        row.account_last4 or row.plaid_mask or row.plaid_account_id for row in posted
    }
    account_keys.discard(None)
    blockers = []
    if not scoped and len(account_keys) > 1:
        blockers.append("Multiple accounts share this source; select one account before verification.")
    if not scoped and account_keys and any(
        not (row.account_last4 or row.plaid_mask or row.plaid_account_id) for row in posted
    ):
        blockers.append("Some ledger rows have no account identity; account scope is incomplete.")
    if scoped:
        unassigned = db.query(Transaction.id).filter(
            Transaction.source == evidence.source, Transaction.currency == currency,
            Transaction.date >= evidence.period_start, Transaction.date <= evidence.period_end,
            Transaction.pending.is_(False), Transaction.account_last4.is_(None),
            Transaction.plaid_mask.is_(None), Transaction.plaid_account_id.is_(None),
        ).count()
        if unassigned:
            blockers.append("Unassigned ledger rows exist for this source and period; review them before verification.")
    # Native-currency facts: the explicit identity conversion rate is exactly one.
    movement = sum((Decimal(str(row._amount)) * Decimal(1) for row in posted), Decimal(0))
    sign = Decimal(-1) if evidence.balance_convention == "debt" else Decimal(1)
    expected_closing = evidence.opening_balance + sign * movement
    difference = evidence.closing_balance - expected_closing
    unmatched_statement = []
    unmatched_ledger = []
    if evidence.transactions is not None:
        available = defaultdict(list)
        for row in posted:
            available[_key(row.date, Decimal(str(row._amount)), row.merchant_raw)].append(row)
        for index, line in enumerate(evidence.transactions):
            matches = available[_key(line.date, line.amount, line.description)]
            if matches:
                matches.pop(0)
            else:
                unmatched_statement.append({"index": index, "date": line.date.isoformat(),
                                            "amount": str(line.amount), "description": line.description})
        for bucket in available.values():
            unmatched_ledger.extend({"id": row.id, "date": row.date.isoformat(),
                                     "amount": str(row._amount), "description": row.merchant_raw,
                                     "origin": row.origin} for row in bucket)
    balanced = difference == Decimal(0)
    if blockers:
        status = "account_review_required"
    elif evidence.transactions is None:
        status = "balance_only" if balanced else "discrepancy"
    elif balanced and not unmatched_statement and not unmatched_ledger:
        status = "verified"
    else:
        status = "discrepancy"
    return {
        "status": status, "currency": currency, "balance_convention": evidence.balance_convention,
        "period_start": evidence.period_start.isoformat(), "period_end": evidence.period_end.isoformat(),
        "ledger_posted_count": len(posted), "pending_excluded_count": len(rows) - len(posted),
        "ledger_movement": str(movement), "expected_closing_balance": str(expected_closing),
        "statement_closing_balance": str(evidence.closing_balance), "difference": str(difference),
        "balance_matches": balanced, "statement_rows_supplied": evidence.transactions is not None,
        "missing_from_ledger": unmatched_statement, "extra_in_ledger": unmatched_ledger,
        "blockers": blockers,
        "comparison": "Exact posted date, signed amount and normalized description; unmatched rows require review.",
    }


def _key(posted_date, amount, description):
    return (posted_date, amount, re.sub(r"\s+", " ", description.strip().casefold()))
