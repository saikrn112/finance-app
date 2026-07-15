from datetime import date
from decimal import Decimal

from src.api.routes.analytics import get_summary, get_trends
from src.models import Transaction


def test_summary_excludes_only_matched_credit_card_payments(temp_db):
    Session, _ = temp_db
    db = Session()
    db.add_all([
        Transaction(
            source_id="salary",
            source="Example Bank",
            date=date(2026, 2, 1),
            amount=Decimal("1000.00"),
            merchant_raw="PAYROLL",
            merchant_clean="Payroll",
            category="Salary/Paycheck",
        ),
        Transaction(
            source_id="example_card-dining",
            source="Example Card",
            date=date(2026, 2, 5),
            amount=Decimal("-50.00"),
            merchant_raw="DOORDASH",
            merchant_clean="DoorDash",
            category="Dining",
        ),
        Transaction(
            source_id="example_bank-payment",
            source="Example Bank",
            date=date(2026, 2, 10),
            amount=Decimal("-200.00"),
            merchant_raw="CHASE CREDIT CRD DES:EPAY",
            merchant_clean="Example Card credit card payment",
            category="Credit Card/Example Card",
        ),
        Transaction(
            source_id="example_card-payment",
            source="Example Card",
            date=date(2026, 2, 11),
            amount=Decimal("200.00"),
            merchant_raw="AUTOMATIC PAYMENT - THANK YOU",
            merchant_clean="Example Card credit card payment",
            category="Credit Card/Example Card",
        ),
        Transaction(
            source_id="example_card-interest",
            source="Example Card",
            date=date(2026, 2, 20),
            amount=Decimal("-10.00"),
            merchant_raw="PURCHASE INTEREST CHARGE",
            merchant_clean="Purchase Interest Charge",
            category="Credit Card/Example Card",
        ),
    ])
    db.commit()

    data = get_summary(db=db, start_date="2026-02-01", end_date="2026-02-28", currency="USD")
    assert data["income"] == 1000.0
    assert data["spending"] == 60.0
    assert data["transfers"] == 200.0
    assert data["net_flow"] == 940.0

    chase_data = get_summary(
        db=db,
        start_date="2026-02-01",
        end_date="2026-02-28",
        source="Example Card",
        currency="USD",
    )
    assert chase_data["income"] == 200.0
    assert chase_data["spending"] == 60.0
    assert chase_data["transfers"] == 0.0
    assert chase_data["net_flow"] == 140.0

    db.close()


def test_trends_keep_unmatched_credit_card_fees_but_hide_payment_pairs(temp_db):
    Session, _ = temp_db
    db = Session()
    db.add_all([
        Transaction(
            source_id="example_bank-payment",
            source="Example Bank",
            date=date(2026, 2, 10),
            amount=Decimal("-200.00"),
            merchant_raw="CHASE CREDIT CRD DES:EPAY",
            merchant_clean="Example Card credit card payment",
            category="Credit Card/Example Card",
        ),
        Transaction(
            source_id="example_card-payment",
            source="Example Card",
            date=date(2026, 2, 11),
            amount=Decimal("200.00"),
            merchant_raw="AUTOMATIC PAYMENT - THANK YOU",
            merchant_clean="Example Card credit card payment",
            category="Credit Card/Example Card",
        ),
        Transaction(
            source_id="example_card-interest",
            source="Example Card",
            date=date(2026, 2, 20),
            amount=Decimal("-10.00"),
            merchant_raw="PURCHASE INTEREST CHARGE",
            merchant_clean="Purchase Interest Charge",
            category="Credit Card/Example Card",
        ),
    ])
    db.commit()

    data = {
        row["period"]: row
        for row in get_trends(
            db=db,
            start_date="2026-02-01",
            end_date="2026-02-28",
            granularity="daily",
            currency="USD",
        )
    }
    assert "2026-02-10" not in data
    assert "2026-02-11" not in data
    assert data["2026-02-20"]["Credit Card/Example Card"] == 10.0
    assert data["2026-02-20"]["total"] == 10.0

    db.close()
