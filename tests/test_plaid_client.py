"""Tests for Plaid client integration."""
import pytest
from unittest.mock import MagicMock
from datetime import date

from src.ingestion.plaid_client import _transform_txn, get_investment_holdings


class TestTransformTxn:
    """Tests for transaction transformation."""

    def test_transforms_basic_fields(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn_abc"
        mock_txn.date = date(2026, 2, 15)
        mock_txn.amount = 100.00
        mock_txn.name = "RAW MERCHANT"
        mock_txn.merchant_name = "Clean Merchant"
        mock_txn.account_id = "account_1234"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["source_id"] == "txn_abc"
        assert result["date"] == "2026-02-15"
        assert result["merchant_raw"] == "RAW MERCHANT"
        assert result["merchant_clean"] == "Clean Merchant"

    def test_negates_amount(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = 50.00  # Plaid: positive = debit
        mock_txn.name = "Merchant"
        mock_txn.merchant_name = None
        mock_txn.account_id = "acc"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["amount"] == -50.00  # Our format: negative = expense

    def test_uses_name_when_merchant_name_missing(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = 10.00
        mock_txn.name = "RAW NAME"
        mock_txn.merchant_name = None
        mock_txn.account_id = "acc"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["merchant_clean"] == "RAW NAME"

    def test_extracts_account_last4(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = 10.00
        mock_txn.name = "Merchant"
        mock_txn.merchant_name = "Merchant"
        mock_txn.account_id = "account_12345678"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["account_last4"] == "5678"

    def test_handles_missing_account_id(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = 10.00
        mock_txn.name = "Merchant"
        mock_txn.merchant_name = "Merchant"
        mock_txn.account_id = None
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["account_last4"] is None

    def test_extracts_plaid_category(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = 10.00
        mock_txn.name = "Merchant"
        mock_txn.merchant_name = "Merchant"
        mock_txn.account_id = "acc"
        mock_txn.personal_finance_category = MagicMock()
        mock_txn.personal_finance_category.primary = "FOOD_AND_DRINK"
        
        result = _transform_txn(mock_txn)
        
        assert result["plaid_category"] == "FOOD_AND_DRINK"

    def test_handles_no_plaid_category(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = 10.00
        mock_txn.name = "Merchant"
        mock_txn.merchant_name = "Merchant"
        mock_txn.account_id = "acc"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["plaid_category"] is None

    def test_handles_string_date(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = "2026-02-15"  # String instead of date object
        mock_txn.amount = 10.00
        mock_txn.name = "Merchant"
        mock_txn.merchant_name = "Merchant"
        mock_txn.account_id = "acc"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["date"] == "2026-02-15"

    def test_handles_negative_plaid_amount(self):
        """Plaid returns negative for credits/refunds."""
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn"
        mock_txn.date = date(2026, 1, 1)
        mock_txn.amount = -100.00  # Plaid: negative = credit
        mock_txn.name = "Refund"
        mock_txn.merchant_name = "Refund"
        mock_txn.account_id = "acc"
        mock_txn.personal_finance_category = None
        
        result = _transform_txn(mock_txn)
        
        assert result["amount"] == 100.00  # Our format: positive = income/refund

    def test_preserves_all_required_fields(self):
        mock_txn = MagicMock()
        mock_txn.transaction_id = "txn_full"
        mock_txn.date = date(2026, 3, 15)
        mock_txn.amount = 75.50
        mock_txn.name = "FULL TEST MERCHANT"
        mock_txn.merchant_name = "Full Test"
        mock_txn.account_id = "acc_99998888"
        mock_txn.personal_finance_category = MagicMock()
        mock_txn.personal_finance_category.primary = "SHOPPING"
        
        result = _transform_txn(mock_txn)
        
        required_fields = ["source_id", "date", "amount", "merchant_raw", 
                          "merchant_clean", "account_last4", "plaid_category"]
        for field in required_fields:
            assert field in result


def test_investment_holdings_uses_serialized_composed_account(monkeypatch):
    account = MagicMock()
    account.to_dict.return_value = {
        "account_id": "brokerage-1",
        "name": "Brokerage",
        "type": "investment",
        "subtype": "brokerage",
        "balances": {"current": 50.0},
    }
    response = MagicMock(securities=[], holdings=[], accounts=[account])
    client = MagicMock()
    client.investments_holdings_get.return_value = response
    monkeypatch.setattr("src.ingestion.plaid_client.get_plaid_client", lambda: client)

    result = get_investment_holdings("token")

    assert result["accounts"] == [{
        "account_id": "brokerage-1",
        "name": "Brokerage",
        "type": "investment",
        "subtype": "brokerage",
        "balance": 50.0,
    }]
