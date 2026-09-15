from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from src.ingestion import plaid_activity
from src.ingestion.plaid_activity import plaid_transactions_destination, reroute_account_activity_to_transactions
from src.ingestion.plaid_sync import apply_plaid_sync_batch
from src.models import AccountActivity, Transaction
from src.processing.overlap_diagnostics import audit_source_overlaps


def test_apply_plaid_sync_batch_updates_modified_transactions(db_session):
    db_session.add(
        Transaction(
            source="Example Card",
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
        institution="Example Card",
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

    txn = db_session.query(Transaction).filter(Transaction.source == "Example Card", Transaction.source_id == "txn-1").one()
    assert counts.updated == 1
    assert txn.date == date(2026, 3, 1)
    assert float(txn._amount) == -23.99
    assert txn.account_last4 == "1234"


def test_plaid_destination_supports_explicit_policy_and_legacy_plugins(monkeypatch):
    plugins = {
        "cash": SimpleNamespace(label="Example Cash", domain="investments", plaid_transactions_destination="transactions"),
        "legacy": SimpleNamespace(label="Example Brokerage", domain="investments"),
    }
    monkeypatch.setattr(plaid_activity, "classify_source", lambda source: "cash" if source == "Example Cash" else "legacy")
    monkeypatch.setattr(plaid_activity, "get_all_sources", lambda: plugins)

    assert plaid_transactions_destination("Example Cash") == "transactions"
    assert plaid_transactions_destination("Example Brokerage") == "account_activity"


def test_reroute_account_activity_moves_only_opted_in_sources(db_session, monkeypatch):
    plugin = SimpleNamespace(
        label="Example Cash", source_aliases=[], domain="investments",
        plaid_transactions_destination="transactions",
    )
    monkeypatch.setattr(plaid_activity, "classify_source", lambda _source: "cash")
    monkeypatch.setattr(plaid_activity, "get_all_sources", lambda: {"cash": plugin})
    db_session.add(AccountActivity(
        source_id="cash-transfer", source_key="cash", source="Example Cash",
        date=date(2026, 9, 7), amount=Decimal("-100.00"),
        description="Transfer to savings", merchant="Transfer to savings",
        activity_type="transfer", currency="USD", pending=False,
        raw_data={"plaid_category": "TRANSFER_OUT"},
    ))
    db_session.commit()

    assert reroute_account_activity_to_transactions(db_session) == {"candidates": 1, "moved": 0, "recategorized": 0}
    result = reroute_account_activity_to_transactions(db_session, apply=True)
    assert result["candidates"] == 1
    assert result["moved"] == 1
    assert db_session.query(AccountActivity).count() == 0
    assert db_session.query(Transaction).filter(Transaction.source_id == "cash-transfer").one().origin == "plaid"


def test_apply_plaid_sync_batch_promotes_pending_transaction(db_session):
    db_session.add(
        Transaction(
            source="Example Charge Card",
            source_id="pending-1",
            origin="plaid",
            date=date(2026, 2, 8),
            amount=Decimal("-132.65"),
            merchant_raw="Example Store",
            merchant_clean="Example Store",
            category="Groceries",
            category_source="rule",
        )
    )
    db_session.commit()

    counts = apply_plaid_sync_batch(
        db_session,
        institution="Example Charge Card",
        added=[
            {
                "source_id": "posted-1",
                "pending_transaction_id": "pending-1",
                "date": date(2026, 2, 9),
                "amount": -132.65,
                "merchant_raw": "Example Store",
                "merchant_clean": "Example Store",
                "account_last4": "2100",
            }
        ],
        modified=[],
        removed=[],
    )
    db_session.commit()

    txns = db_session.query(Transaction).filter(Transaction.source == "Example Charge Card", Transaction.origin == "plaid").all()
    assert counts.pending_promotions == 1
    assert len(txns) == 1
    assert txns[0].source_id == "posted-1"
    assert txns[0].date == date(2026, 2, 9)


def test_overlap_audit_finds_statement_vs_plaid_duplicate(db_session):
    db_session.add_all(
        [
            Transaction(
                source="Example Charge Card",
                source_id="stmt-1",
                origin="statements",
                date=date(2026, 2, 7),
                amount=Decimal("-132.65"),
                merchant_raw="EXAMPLE STORE 1234 ANYTOWN CA",
                merchant_clean="Example Store",
                category="Groceries",
                category_source="rule",
            ),
            Transaction(
                source="Example Charge Card",
                source_id="plaid-1",
                origin="plaid",
                date=date(2026, 2, 8),
                amount=Decimal("-132.65"),
                merchant_raw="Example Store",
                merchant_clean="Example Store",
                category="Groceries",
                category_source="rule",
            ),
        ]
    )
    db_session.commit()

    audit = audit_source_overlaps(db_session, "Example Charge Card")
    assert audit["cross_source_overlap_total"] == 132.65
    assert len(audit["cross_source_overlaps"]) == 1
    assert audit["cross_source_overlaps"][0]["date_diff_days"] == 1


def test_overlap_audit_finds_plaid_pending_duplicates(db_session):
    db_session.add_all(
        [
            Transaction(
                source="Example Card",
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
                source="Example Card",
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

    audit = audit_source_overlaps(db_session, "Example Card")
    assert audit["plaid_duplicate_total"] == 156.34
    assert len(audit["plaid_duplicates"]) == 1


def test_apply_plaid_sync_batch_stores_pending_then_promotes_it(db_session):
    """Pending rows are kept, then promoted in place when they post.

    This used to assert that pending rows were skipped entirely. That behaviour is gone:
    pending charges are stored so they show up promptly, and `pending_transaction_id` links
    the posted row back so it is promoted rather than duplicated. Anything that keys off the
    row (category, project, split) therefore survives the transition.
    """
    pending = {
        "source_id": "pending-ride",
        "pending_transaction_id": None,
        "pending": True,
        "date": date(2026, 3, 7),
        "amount": -23.99,
        "merchant_raw": "Example Rideshare",
        "merchant_clean": "Example Rideshare",
        "account_last4": "1234",
    }
    counts = apply_plaid_sync_batch(
        db_session, institution="Example Card", added=[pending], modified=[], removed=[],
    )
    db_session.commit()

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Card").all()
    assert counts.added == 1
    assert len(rows) == 1
    assert rows[0].pending is True

    # The posted charge arrives with a new id that points back at the pending one.
    posted = {
        **pending,
        "source_id": "posted-ride",
        "pending_transaction_id": "pending-ride",
        "pending": False,
        "date": date(2026, 3, 9),
    }
    counts = apply_plaid_sync_batch(
        db_session, institution="Example Card", added=[posted], modified=[], removed=[],
    )
    db_session.commit()

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Card").all()
    assert counts.pending_promotions == 1
    assert counts.added == 0, "the posted charge must not become a second row"
    assert len(rows) == 1
    assert rows[0].source_id == "posted-ride"
    assert rows[0].pending is False

def test_apply_plaid_sync_batch_skips_statement_boundary_overlap(db_session):
    db_session.add(
        Transaction(
            source="Example Charge Card",
            source_id="stmt-1",
            origin="statements",
            date=date(2026, 2, 7),
            amount=Decimal("-132.65"),
            merchant_raw="EXAMPLE STORE 1234 ANYTOWN CA",
            merchant_clean="Example Store",
            category="Groceries",
            category_source="rule",
        )
    )
    db_session.commit()

    counts = apply_plaid_sync_batch(
        db_session,
        institution="Example Charge Card",
        added=[
            {
                "source_id": "plaid-dup",
                "pending_transaction_id": None,
                "pending": False,
                "date": date(2026, 2, 8),
                "amount": -132.65,
                "merchant_raw": "Example Store",
                "merchant_clean": "Example Store",
                "account_last4": "2100",
            }
        ],
        modified=[],
        removed=[],
    )
    db_session.commit()

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Charge Card", Transaction.origin == "plaid").all()
    assert counts.skipped_statement_overlap == 1
    assert rows == []


def test_replaced_plaid_item_reuses_enriched_transaction(db_session):
    original = Transaction(
        source="Example Card",
        source_id="retired-item-txn",
        origin="plaid",
        plaid_account_id="retired-account-id",
        account_last4="1234",
        date=date(2026, 7, 12),
        amount=Decimal("-17.39"),
        merchant_raw="Example Store",
        merchant_clean="Example Store",
        category="Groceries/Specialty",
        category_source="user",
        notes="keep this",
    )
    db_session.add(original)
    db_session.commit()
    original_id = original.id

    counts = apply_plaid_sync_batch(
        db_session,
        institution="Example Card",
        added=[{
            "source_id": "surviving-item-txn",
            "pending_transaction_id": None,
            "pending": False,
            "plaid_account_id": "surviving-account-id",
            "account_last4": "1234",
            "date": date(2026, 7, 12),
            "amount": -17.39,
            "merchant_raw": "Example Store",
            "merchant_clean": "Example Store",
        }],
        modified=[],
        removed=[],
    )
    db_session.commit()

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Card").all()
    assert counts.updated == 1
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].source_id == "surviving-item-txn"
    assert rows[0].plaid_account_id == "surviving-account-id"
    assert rows[0].category == "Groceries/Specialty"
    assert rows[0].category_source == "user"
    assert rows[0].notes == "keep this"


def test_same_item_equal_transactions_remain_distinct(db_session):
    db_session.add(Transaction(
        source="Example Card",
        source_id="first-fare",
        origin="plaid",
        plaid_account_id="same-account-id",
        account_last4="1234",
        date=date(2026, 7, 6),
        amount=Decimal("-20.00"),
        merchant_raw="Transit Fare",
        merchant_clean="Transit Fare",
        category="Transportation/Transit",
        category_source="user",
    ))
    db_session.commit()

    apply_plaid_sync_batch(
        db_session,
        institution="Example Card",
        added=[{
            "source_id": "second-fare",
            "pending_transaction_id": None,
            "pending": False,
            "plaid_account_id": "same-account-id",
            "account_last4": "1234",
            "date": date(2026, 7, 6),
            "amount": -20.00,
            "merchant_raw": "Transit Fare",
            "merchant_clean": "Transit Fare",
        }],
        modified=[],
        removed=[],
    )
    db_session.commit()

    assert db_session.query(Transaction).filter(Transaction.source == "Example Card").count() == 2
