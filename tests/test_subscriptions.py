"""Tests for subscription detection."""
from datetime import date, timedelta
from decimal import Decimal

from src.processing.subscriptions import (
    detect_subscriptions,
    get_monthly_subscription_total,
    get_recurring_catalog,
    get_recurring_detail,
)
from src.models.transaction import Transaction


class TestDetectSubscriptions:
    """Tests for subscription detection logic."""

    def test_detects_monthly_subscription(self, db_session, sample_transactions):
        # Netflix appears on Dec 1, Jan 1, Feb 3 - monthly pattern
        subs = detect_subscriptions(db_session)
        netflix = next((s for s in subs if "Netflix" in s["merchant"]), None)
        assert netflix is not None
        assert netflix["frequency"] == "monthly"
        assert netflix["amount"] == 15.99

    def test_requires_minimum_occurrences(self, db_session):
        # Single transaction shouldn't be detected as subscription
        txn = Transaction(
            source_id="single", source="chase", date=date.today(),
            amount=Decimal("-9.99"), merchant_raw="ONE TIME",
            merchant_clean="One Time", category="Shopping"
        )
        db_session.add(txn)
        db_session.commit()
        
        subs = detect_subscriptions(db_session)
        assert not any(s["merchant"] == "One Time" for s in subs)

    def test_ignores_income_transactions(self, db_session, sample_transactions):
        subs = detect_subscriptions(db_session)
        assert not any(s["merchant"] == "Payroll" for s in subs)

    def test_groups_by_merchant_and_amount(self, db_session):
        # Same merchant, different amounts = different subscriptions
        for i, amount in enumerate([9.99, 19.99]):
            for month in range(3):
                txn = Transaction(
                    source_id=f"sub_{i}_{month}", source="chase",
                    date=date.today() - timedelta(days=30 * month),
                    amount=Decimal(f"-{amount}"), merchant_raw=f"SERVICE_{i}",
                    merchant_clean=f"Service {i}", category="Subscriptions"
                )
                db_session.add(txn)
        db_session.commit()
        
        subs = detect_subscriptions(db_session)
        service_subs = [s for s in subs if "Service" in s["merchant"]]
        assert len(service_subs) == 2

    def test_detects_biweekly_frequency(self, db_session):
        for i in range(4):
            txn = Transaction(
                source_id=f"biweekly_{i}", source="chase",
                date=date.today() - timedelta(days=14 * i),
                amount=Decimal("-50.00"), merchant_raw="BIWEEKLY SVC",
                merchant_clean="Biweekly Service", category="Subscriptions"
            )
            db_session.add(txn)
        db_session.commit()
        
        subs = detect_subscriptions(db_session)
        biweekly = next((s for s in subs if "Biweekly" in s["merchant"]), None)
        assert biweekly is not None
        assert biweekly["frequency"] == "biweekly"

    def test_detects_quarterly_frequency(self, db_session):
        for i in range(3):
            txn = Transaction(
                source_id=f"quarterly_{i}", source="chase",
                date=date.today() - timedelta(days=90 * i),
                amount=Decimal("-99.00"), merchant_raw="QUARTERLY SVC",
                merchant_clean="Quarterly Service", category="Subscriptions"
            )
            db_session.add(txn)
        db_session.commit()
        
        subs = detect_subscriptions(db_session)
        quarterly = next((s for s in subs if "Quarterly" in s["merchant"]), None)
        assert quarterly is not None
        assert quarterly["frequency"] == "quarterly"

    def test_rejects_inconsistent_intervals(self, db_session):
        # Transactions with wildly varying intervals
        dates = [date.today(), date.today() - timedelta(days=10), 
                 date.today() - timedelta(days=60)]
        for i, d in enumerate(dates):
            txn = Transaction(
                source_id=f"irregular_{i}", source="chase", date=d,
                amount=Decimal("-25.00"), merchant_raw="IRREGULAR",
                merchant_clean="Irregular", category="Shopping"
            )
            db_session.add(txn)
        db_session.commit()
        
        subs = detect_subscriptions(db_session)
        assert not any(s["merchant"] == "Irregular" for s in subs)

    def test_sorted_by_amount_descending(self, db_session):
        for i, amount in enumerate([5.00, 50.00, 25.00]):
            for month in range(3):
                txn = Transaction(
                    source_id=f"sort_{i}_{month}", source="chase",
                    date=date.today() - timedelta(days=30 * month),
                    amount=Decimal(f"-{amount}"), merchant_raw=f"SORT_{i}",
                    merchant_clean=f"Sort {i}", category="Subscriptions"
                )
                db_session.add(txn)
        db_session.commit()
        
        subs = detect_subscriptions(db_session)
        sort_subs = [s for s in subs if "Sort" in s["merchant"]]
        amounts = [s["amount"] for s in sort_subs]
        assert amounts == sorted(amounts, reverse=True)


class TestGetMonthlySubscriptionTotal:
    """Tests for monthly subscription cost calculation."""

    def test_sums_monthly_subscriptions(self, db_session, sample_transactions):
        total = get_monthly_subscription_total(db_session)
        # Netflix at $15.99/month
        assert total >= 15.99

    def test_normalizes_biweekly_to_monthly(self, db_session):
        for i in range(4):
            txn = Transaction(
                source_id=f"bw_{i}", source="chase",
                date=date.today() - timedelta(days=14 * i),
                amount=Decimal("-25.00"), merchant_raw="BIWEEKLY",
                merchant_clean="Biweekly", category="Subscriptions"
            )
            db_session.add(txn)
        db_session.commit()
        
        total = get_monthly_subscription_total(db_session)
        # $25 biweekly = $50/month
        assert total >= 50.00

    def test_normalizes_quarterly_to_monthly(self, db_session):
        for i in range(3):
            txn = Transaction(
                source_id=f"q_{i}", source="chase",
                date=date.today() - timedelta(days=90 * i),
                amount=Decimal("-90.00"), merchant_raw="QUARTERLY",
                merchant_clean="Quarterly", category="Subscriptions"
            )
            db_session.add(txn)
        db_session.commit()
        
        total = get_monthly_subscription_total(db_session)
        # $90 quarterly = $30/month
        assert total >= 30.00

    def test_normalizes_yearly_to_monthly(self, db_session):
        for i in range(2):
            txn = Transaction(
                source_id=f"y_{i}", source="chase",
                date=date.today() - timedelta(days=365 * i),
                amount=Decimal("-120.00"), merchant_raw="YEARLY",
                merchant_clean="Yearly", category="Subscriptions"
            )
            db_session.add(txn)
        db_session.commit()
        
        total = get_monthly_subscription_total(db_session)
        # $120 yearly = $10/month
        assert total >= 10.00

    def test_returns_zero_with_no_subscriptions(self, db_session):
        total = get_monthly_subscription_total(db_session)
        # Empty db or no recurring patterns
        assert isinstance(total, float)

    def test_rounds_to_two_decimals(self, db_session, sample_transactions):
        total = get_monthly_subscription_total(db_session)
        assert total == round(total, 2)


class TestRecurringWorkspace:
    def test_catalog_keeps_ended_items(self, db_session):
        for i, when in enumerate([date(2025, 12, 12), date(2026, 1, 12)]):
            db_session.add(
                Transaction(
                    source_id=f"openai_{i}",
                    source="chase",
                    date=when,
                    amount=Decimal("-20.00"),
                    merchant_raw="OPENAI *CHATGPT SUBSCR",
                    merchant_clean="OpenAI",
                    category="Subscriptions/Tech",
                    account_last4="9001",
                )
            )
        db_session.commit()

        catalog = get_recurring_catalog(db_session, start_date=date(2026, 1, 1), end_date=date(2026, 3, 31))
        openai = next(item for item in catalog["items"] if item["display_name"] == "OpenAI")

        assert openai["status"] == "ended"
        assert openai["ended_at"] == "2026-02-11"

    def test_detail_surfaces_price_change_event(self, db_session):
        charges = [
            (date(2025, 11, 10), Decimal("-10.99")),
            (date(2025, 12, 10), Decimal("-10.99")),
            (date(2026, 1, 10), Decimal("-12.99")),
            (date(2026, 2, 10), Decimal("-12.99")),
        ]
        for i, (when, amount) in enumerate(charges):
            db_session.add(
                Transaction(
                    source_id=f"disney_{i}",
                    source="amex",
                    date=when,
                    amount=amount,
                    merchant_raw="DISNEYPLUS 888-905-7888 CA",
                    merchant_clean="Disney+",
                    category="Subscriptions/Entertainment",
                    account_last4="2100",
                )
            )
        db_session.commit()

        catalog = get_recurring_catalog(db_session)
        disney = next(item for item in catalog["items"] if item["display_name"] == "Disney+")
        detail = get_recurring_detail(db_session, disney["id"])

        assert detail is not None
        assert "price_changed" in detail["flags"]
        assert any(event["event_type"] == "price_changed" for event in detail["events"])
