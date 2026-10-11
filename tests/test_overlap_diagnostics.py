from datetime import date
from decimal import Decimal

import pytest

from src.models import Transaction
from src.processing.overlap_diagnostics import audit_source_overlaps


def add(db, key, **changes):
    row = dict(source="Synthetic Bank", source_id=key, date=date(2026, 1, 15),
               amount=Decimal("-10"), merchant_raw="Synthetic Shop", account_last4="1001",
               currency="USD", origin="plaid", pending=False)
    row.update(changes)
    db.add(Transaction(**row))
    db.commit()


def test_overlap_candidates_require_same_account_currency_and_sign(db_session):
    add(db_session, "statement", origin="statements")
    add(db_session, "other-account", account_last4="1002")
    add(db_session, "other-currency", currency="EUR")
    add(db_session, "opposite-sign", amount=Decimal("10"))
    add(db_session, "unknown-account", account_last4=None)
    assert audit_source_overlaps(db_session, "Synthetic Bank")["cross_source_overlaps"] == []


def test_overlap_matching_uses_each_candidate_once_and_is_not_a_delete_instruction(db_session):
    add(db_session, "statement", origin="statements")
    add(db_session, "plaid-1")
    add(db_session, "plaid-2")
    result = audit_source_overlaps(db_session, "Synthetic Bank")
    assert len(result["cross_source_overlaps"]) == 1
    assert result["cross_source_overlaps"][0]["candidate_only"] is True
    assert db_session.query(Transaction).count() == 3


def test_mixed_currency_totals_and_candidates_use_each_rows_exchange_rate(db_session):
    add(db_session, "usd", origin="statements")
    add(db_session, "eur", currency="EUR", origin="statements")
    add(db_session, "eur-plaid", currency="EUR")
    result = audit_source_overlaps(db_session, "Synthetic Bank", rate_map={"USD": 1, "EUR": 2})
    assert result["db_balance"] == -50
    assert result["statement_balance"] == -30
    assert result["cross_source_overlap_total"] == 20
    assert result["cross_source_overlaps"][0]["amount"] == -20


def test_missing_exchange_rate_does_not_silently_assume_identity(db_session):
    add(db_session, "eur", currency="EUR")
    with pytest.raises(ValueError, match="EUR"):
        audit_source_overlaps(db_session, "Synthetic Bank", rate_map={"USD": 1})
