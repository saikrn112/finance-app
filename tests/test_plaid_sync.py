from datetime import date
from decimal import Decimal

from src.ingestion.plaid_sync import apply_plaid_sync_batch
from src.models import Transaction
from src.processing.overlap_diagnostics import audit_source_overlaps


def test_apply_plaid_sync_batch_updates_modified_transactions(db_session):
    db_session.add(
        Transaction(
            source="Chase",
            source_id="txn-1",
            origin="plaid",
            date=date(2026, 2, 27),
            amount=Decimal("-23.96"),
            merchant_raw="Lyft",
            merchant_clean="Lyft",
            category="Transportation",
            category_source="rule",
        )
    )
    db_session.commit()

    counts = apply_plaid_sync_batch(
        db_session,
        institution="Chase",
        added=[],
        modified=[
            {
                "source_id": "txn-1",
                "pending_transaction_id": None,
                "date": date(2026, 3, 1),
                "amount": -23.99,
                "merchant_raw": "Lyft",
                "merchant_clean": "Lyft",
                "account_last4": "1234",
            }
        ],
        removed=[],
    )
    db_session.commit()

    txn = db_session.query(Transaction).filter(Transaction.source == "Chase", Transaction.source_id == "txn-1").one()
    assert counts.updated == 1
    assert txn.date == date(2026, 3, 1)
    assert float(txn._amount) == -23.99
    assert txn.account_last4 == "1234"


def test_apply_plaid_sync_batch_promotes_pending_transaction(db_session):
    db_session.add(
        Transaction(
            source="American Express",
            source_id="pending-1",
            origin="plaid",
            date=date(2026, 2, 8),
            amount=Decimal("-132.65"),
            merchant_raw="Walmart",
            merchant_clean="Walmart",
            category="Groceries",
            category_source="rule",
        )
    )
    db_session.commit()

    counts = apply_plaid_sync_batch(
        db_session,
        institution="American Express",
        added=[
            {
                "source_id": "posted-1",
                "pending_transaction_id": "pending-1",
                "date": date(2026, 2, 9),
                "amount": -132.65,
                "merchant_raw": "Walmart",
                "merchant_clean": "Walmart",
                "account_last4": "2100",
            }
        ],
        modified=[],
        removed=[],
    )
    db_session.commit()

    txns = db_session.query(Transaction).filter(Transaction.source == "American Express", Transaction.origin == "plaid").all()
    assert counts.pending_promotions == 1
    assert len(txns) == 1
    assert txns[0].source_id == "posted-1"
    assert txns[0].date == date(2026, 2, 9)


def test_overlap_audit_finds_statement_vs_plaid_duplicate(db_session):
    db_session.add_all(
        [
            Transaction(
                source="American Express",
                source_id="stmt-1",
                origin="statements",
                date=date(2026, 2, 7),
                amount=Decimal("-132.65"),
                merchant_raw="WAL-MART NEIGHBORHOOD MARKET 1234 ANYTOWN CA",
                merchant_clean="Walmart",
                category="Groceries",
                category_source="rule",
            ),
            Transaction(
                source="American Express",
                source_id="plaid-1",
                origin="plaid",
                date=date(2026, 2, 8),
                amount=Decimal("-132.65"),
                merchant_raw="Walmart",
                merchant_clean="Walmart",
                category="Groceries",
                category_source="rule",
            ),
        ]
    )
    db_session.commit()

    audit = audit_source_overlaps(db_session, "American Express")
    assert audit["cross_source_overlap_total"] == 132.65
    assert len(audit["cross_source_overlaps"]) == 1
    assert audit["cross_source_overlaps"][0]["date_diff_days"] == 1


def test_overlap_audit_finds_plaid_pending_duplicates(db_session):
    db_session.add_all(
        [
            Transaction(
                source="Chase",
                source_id="plaid-a",
                origin="plaid",
                date=date(2026, 3, 2),
                amount=Decimal("-156.34"),
                merchant_raw="ASI Select Insurance Co",
                merchant_clean="ASI Select Insurance Co",
                category="Insurance",
                category_source="rule",
            ),
            Transaction(
                source="Chase",
                source_id="plaid-b",
                origin="plaid",
                date=date(2026, 3, 3),
                amount=Decimal("-156.34"),
                merchant_raw="ASI SELECT INSURANCE CO",
                merchant_clean="ASI Select Insurance Co",
                category="Insurance",
                category_source="rule",
            ),
        ]
    )
    db_session.commit()

    audit = audit_source_overlaps(db_session, "Chase")
    assert audit["plaid_duplicate_total"] == 156.34
    assert len(audit["plaid_duplicates"]) == 1


def test_apply_plaid_sync_batch_skips_pending_rows(db_session):
    counts = apply_plaid_sync_batch(
        db_session,
        institution="Chase",
        added=[
            {
                "source_id": "pending-lyft",
                "pending_transaction_id": None,
                "pending": True,
                "date": date(2026, 3, 7),
                "amount": -23.99,
                "merchant_raw": "Lyft",
                "merchant_clean": "Lyft",
                "account_last4": "1234",
            }
        ],
        modified=[],
        removed=[],
    )
    db_session.commit()

    rows = db_session.query(Transaction).filter(Transaction.source == "Chase").all()
    assert counts.skipped_pending == 1
    assert rows == []


def test_apply_plaid_sync_batch_skips_statement_boundary_overlap(db_session):
    db_session.add(
        Transaction(
            source="American Express",
            source_id="stmt-1",
            origin="statements",
            date=date(2026, 2, 7),
            amount=Decimal("-132.65"),
            merchant_raw="WAL-MART NEIGHBORHOOD MARKET 1234 ANYTOWN CA",
            merchant_clean="Walmart",
            category="Groceries",
            category_source="rule",
        )
    )
    db_session.commit()

    counts = apply_plaid_sync_batch(
        db_session,
        institution="American Express",
        added=[
            {
                "source_id": "plaid-dup",
                "pending_transaction_id": None,
                "pending": False,
                "date": date(2026, 2, 8),
                "amount": -132.65,
                "merchant_raw": "Walmart",
                "merchant_clean": "Walmart",
                "account_last4": "2100",
            }
        ],
        modified=[],
        removed=[],
    )
    db_session.commit()

    rows = db_session.query(Transaction).filter(Transaction.source == "American Express", Transaction.origin == "plaid").all()
    assert counts.skipped_statement_overlap == 1
    assert rows == []
