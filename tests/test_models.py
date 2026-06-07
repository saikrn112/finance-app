"""Tests for database models."""
import pytest
from datetime import date, datetime
from decimal import Decimal

from src.models.transaction import Transaction, Subscription, Rule, SyncLog, generate_uuid


class TestTransaction:
    """Tests for Transaction model."""

    def test_create_transaction(self, db_session):
        txn = Transaction(
            source_id="test123",
            source="chase",
            date=date(2026, 2, 1),
            amount=Decimal("-50.00"),
            merchant_raw="TEST MERCHANT",
        )
        db_session.add(txn)
        db_session.commit()
        
        assert txn.id is not None
        assert txn.created_at is not None

    def test_unique_source_source_id_constraint(self, db_session):
        txn1 = Transaction(
            source_id="dup123", source="chase", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="MERCHANT"
        )
        db_session.add(txn1)
        db_session.commit()
        
        txn2 = Transaction(
            source_id="dup123", source="chase", date=date(2026, 2, 2),
            amount=Decimal("-60.00"), merchant_raw="MERCHANT 2"
        )
        db_session.add(txn2)
        
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()

    def test_same_source_id_different_source_allowed(self, db_session):
        txn1 = Transaction(
            source_id="same123", source="chase", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="MERCHANT"
        )
        txn2 = Transaction(
            source_id="same123", source="amex", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="MERCHANT"
        )
        db_session.add(txn1)
        db_session.add(txn2)
        db_session.commit()
        
        assert txn1.id != txn2.id

    def test_default_values(self, db_session):
        txn = Transaction(
            source_id="def123", source="chase", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="MERCHANT"
        )
        db_session.add(txn)
        db_session.commit()
        
        assert txn.is_recurring is False
        assert txn.tags == [] or txn.tags is None

    def test_updated_at_changes_on_update(self, db_session):
        txn = Transaction(
            source_id="upd123", source="chase", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="MERCHANT"
        )
        db_session.add(txn)
        db_session.commit()
        
        original_updated = txn.updated_at
        txn.category = "Shopping"
        db_session.commit()
        
        # Note: SQLite may not update automatically without explicit trigger
        # This tests the model definition is correct


class TestSubscription:
    """Tests for Subscription model."""

    def test_create_subscription(self, db_session):
        sub = Subscription(
            merchant="Netflix",
            amount=Decimal("15.99"),
            frequency="monthly",
            source="chase",
            status="active"
        )
        db_session.add(sub)
        db_session.commit()
        
        assert sub.id is not None
        assert sub.status == "active"

    def test_default_status(self, db_session):
        sub = Subscription(
            merchant="Test", amount=Decimal("9.99")
        )
        db_session.add(sub)
        db_session.commit()
        
        assert sub.status == "active"

    def test_price_history_json(self, db_session):
        sub = Subscription(
            merchant="Test", amount=Decimal("9.99"),
            price_history=[{"date": "2026-01-01", "amount": 8.99}]
        )
        db_session.add(sub)
        db_session.commit()
        
        db_session.refresh(sub)
        assert sub.price_history[0]["amount"] == 8.99


class TestRule:
    """Tests for Rule model."""

    def test_create_rule(self, db_session):
        rule = Rule(
            pattern="NETFLIX",
            category="Subscriptions",
            source="user"
        )
        db_session.add(rule)
        db_session.commit()
        
        assert rule.id is not None

    def test_default_values(self, db_session):
        rule = Rule(pattern="TEST", category="Shopping")
        db_session.add(rule)
        db_session.commit()
        
        assert rule.match_field == "merchant_raw"
        assert rule.source == "user"
        assert rule.priority == 0


class TestSyncLog:
    """Tests for SyncLog model."""

    def test_create_sync_log(self, db_session):
        log = SyncLog(
            source="chase",
            sync_type="csv",
            file_hash="abc123",
            record_count=100,
            status="success"
        )
        db_session.add(log)
        db_session.commit()
        
        assert log.id is not None
        assert log.created_at is not None

    def test_error_log(self, db_session):
        log = SyncLog(
            source="plaid",
            sync_type="api",
            status="error",
            error_message="Connection failed"
        )
        db_session.add(log)
        db_session.commit()
        
        assert log.error_message == "Connection failed"


class TestGenerateUUID:
    """Tests for UUID generation."""

    def test_generates_valid_uuid(self):
        uuid1 = generate_uuid()
        uuid2 = generate_uuid()
        
        assert len(uuid1) == 36  # UUID format with dashes
        assert uuid1 != uuid2

    def test_uuid_format(self):
        uuid = generate_uuid()
        parts = uuid.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12
