"""Statement evidence invariants; all names, identifiers and values are synthetic."""
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models import Transaction
from src.processing.statement_verification import StatementEvidence, verify_statement


def evidence(**changes):
    payload = dict(source="Synthetic Bank", account_last4="1001", period_start="2026-01-01",
                   period_end="2026-01-31", opening_balance="100", closing_balance="90",
                   transactions=[dict(date="2026-01-15", amount="-10", description="Synthetic Shop")])
    payload.update(changes)
    return StatementEvidence(**payload)


def add(db, **changes):
    values = dict(source="Synthetic Bank", source_id="synthetic-row", account_last4="1001",
                  date=date(2026, 1, 15), amount=Decimal("-10"), merchant_raw="Synthetic Shop",
                  currency="USD", origin="plaid", pending=False)
    values.update(changes)
    row = Transaction(**values)
    db.add(row)
    db.commit()
    return row


def test_exact_period_evidence_verifies_without_writes(db_session):
    row = add(db_session)
    before = row.updated_at
    result = verify_statement(db_session, evidence(), "USD")
    assert result["status"] == "verified"
    assert Decimal(result["difference"]) == 0
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    assert row.updated_at == before


def test_equal_balance_cannot_hide_missing_and_extra_rows(db_session):
    add(db_session, merchant_raw="Another Synthetic Shop")
    result = verify_statement(db_session, evidence(), "USD")
    assert result["balance_matches"]
    assert result["status"] == "discrepancy"
    assert len(result["missing_from_ledger"]) == len(result["extra_in_ledger"]) == 1


def test_repeated_statement_rows_require_distinct_ledger_rows(db_session):
    add(db_session)
    line = dict(date="2026-01-15", amount="-10", description="Synthetic Shop")
    result = verify_statement(db_session, evidence(closing_balance="80", transactions=[line, line]), "USD")
    assert result["status"] == "discrepancy"
    assert len(result["missing_from_ledger"]) == 1
    assert Decimal(result["difference"]) == -10


def test_pending_other_account_currency_and_outside_period_are_excluded(db_session):
    add(db_session)
    add(db_session, source_id="pending", pending=True)
    add(db_session, source_id="other-account", account_last4="1002")
    add(db_session, source_id="other-currency", currency="EUR")
    add(db_session, source_id="outside", date=date(2025, 12, 31))
    result = verify_statement(db_session, evidence(), "USD")
    assert result["status"] == "verified"
    assert result["ledger_posted_count"] == result["pending_excluded_count"] == 1


def test_multiple_accounts_require_explicit_scope(db_session):
    add(db_session)
    add(db_session, source_id="other-account", account_last4="1002")
    result = verify_statement(db_session, evidence(account_last4=None), "USD")
    assert result["status"] == "account_review_required"
    assert result["blockers"]


def test_unassigned_account_rows_block_scoped_verification(db_session):
    add(db_session)
    add(db_session, source_id="unassigned", account_last4=None)
    assert verify_statement(db_session, evidence(), "USD")["status"] == "account_review_required"


def test_credit_card_debt_convention_is_explicit(db_session):
    add(db_session)
    result = verify_statement(db_session, evidence(balance_convention="debt", closing_balance="110"), "USD")
    assert result["status"] == "verified"
    assert Decimal(result["expected_closing_balance"]) == 110


def test_balance_only_is_never_full_verification(db_session):
    add(db_session)
    assert verify_statement(db_session, evidence(transactions=None), "USD")["status"] == "balance_only"


def test_decimal_cent_difference_is_visible(db_session):
    add(db_session)
    result = verify_statement(db_session, evidence(closing_balance="90.01"), "USD")
    assert Decimal(result["difference"]) == Decimal("0.01")
    assert result["status"] == "discrepancy"


@pytest.mark.parametrize("changes", [
    {"period_end": "2025-12-31"},
    {"transactions": [dict(date="2026-02-01", amount="-10", description="Synthetic Shop")]},
    {"opening_balance": "NaN"}, {"closing_balance": "Infinity"},
])
def test_invalid_evidence_rejected(changes):
    with pytest.raises(ValidationError):
        evidence(**changes)
