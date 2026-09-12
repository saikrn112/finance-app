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

    # mask and currency are part of the serialized account: mask distinguishes two
    # accounts at the same institution, currency keeps conversion honest.
    assert result["accounts"] == [{
        "account_id": "brokerage-1",
        "name": "Brokerage",
        "type": "investment",
        "subtype": "brokerage",
        "balance": 50.0,
        "mask": None,
        "currency": "USD",
    }]


class TestCreateLinkTokenUpdateMode:
    """Plaid forbids `products` in update mode.

    Sending both an access_token and products makes Plaid ask for consent the user never
    granted (ADDITIONAL_CONSENT_REQUIRED), so a re-auth can never complete and the Item
    stays stuck in ITEM_LOGIN_REQUIRED.
    """

    def _captured_request(self, monkeypatch, **kwargs):
        captured = {}

        class FakeClient:
            def link_token_create(self, request):
                captured["request"] = request
                class Resp:
                    link_token = "link-sandbox-test"
                return Resp()

        monkeypatch.setattr("src.ingestion.plaid_client.get_plaid_client", lambda: FakeClient())
        from src.ingestion.plaid_client import create_link_token
        create_link_token(**kwargs)
        return captured["request"]

    def test_update_mode_omits_products(self, monkeypatch):
        request = self._captured_request(
            monkeypatch, products=["transactions", "investments"], access_token="access-prod-abc"
        )
        assert request.get("access_token") == "access-prod-abc"
        assert "products" not in request
        assert request["update"].account_selection_enabled is True

    def test_initial_link_still_sends_products(self, monkeypatch):
        request = self._captured_request(monkeypatch, products=["transactions"])
        assert "access_token" not in request
        assert [str(p) for p in request["products"]] == ["transactions"]


class TestDeadItemHandling:
    """A Plaid Item that no longer exists cannot be repaired by Reconnect.

    Regression guard: a re-link of an already-connected institution used to revoke whatever
    Item Plaid handed back. When Plaid returns the *existing* Item for that institution,
    that revoked the live connection and left the stored token pointing at nothing — which
    only showed up later as ITEM_NOT_FOUND on a row still labelled "connected".

    These use the `db_session` fixture (a throwaway SQLite file). `SessionLocal` resolves to
    the real configured database even under pytest, so it must not be used here.
    """

    def test_plaid_error_code_is_extracted_from_the_body(self):
        from src.api.routes.sync import _plaid_error_code

        class Exc(Exception):
            body = '{"error_code": "ITEM_NOT_FOUND", "error_message": "gone"}'

        assert _plaid_error_code(Exc()) == "ITEM_NOT_FOUND"

    def test_plaid_error_code_tolerates_a_missing_or_junk_body(self):
        from src.api.routes.sync import _plaid_error_code

        class NoBody(Exception):
            pass

        class JunkBody(Exception):
            body = "not json"

        assert _plaid_error_code(NoBody()) is None
        assert _plaid_error_code(JunkBody()) is None

    def test_duplicate_relink_does_not_revoke_an_item_we_already_have(self, db_session, monkeypatch):
        """The core fix: same Item back from Plaid => do not call /item/remove."""
        from fastapi import HTTPException
        from src.api.routes import sync as sync_routes
        from src.models import SyncLog

        removed: list[str] = []
        monkeypatch.setattr(sync_routes, "remove_item", lambda token, **kw: removed.append(token))
        monkeypatch.setattr(sync_routes, "exchange_public_token",
                            lambda _pt: ("access-prod-live", "item-already-ours", ""))
        monkeypatch.setattr(sync_routes, "_has_duplicate_connection", lambda *a, **k: True)

        db_session.add(SyncLog(source="plaid", sync_type="plaid", status="connected",
                               plaid_item_id="item-already-ours",
                               extra_data={"institution_name": "Test Bank",
                                           "access_token": "access-prod-live"}))
        db_session.commit()

        with pytest.raises(HTTPException) as caught:
            sync_routes.exchange_token(public_token="public-x",
                                       institution_name="Test Bank", db=db_session)
        assert caught.value.status_code == 409
        assert removed == [], "must not revoke an Item that is already ours"

    def test_duplicate_relink_still_revokes_a_genuinely_new_item(self, db_session, monkeypatch):
        from fastapi import HTTPException
        from src.api.routes import sync as sync_routes

        removed: list[str] = []
        monkeypatch.setattr(sync_routes, "remove_item", lambda token, **kw: removed.append(token))
        monkeypatch.setattr(sync_routes, "exchange_public_token",
                            lambda _pt: ("access-prod-new", "item-brand-new", ""))
        monkeypatch.setattr(sync_routes, "_has_duplicate_connection", lambda *a, **k: True)

        with pytest.raises(HTTPException) as caught:
            sync_routes.exchange_token(public_token="public-y",
                                       institution_name="Test Bank", db=db_session)
        assert caught.value.status_code == 409
        assert removed == ["access-prod-new"]

    def test_item_gone_is_recorded_and_reported_as_conflict(self, db_session, monkeypatch):
        """A failed reconnect flags the row so the UI can stop offering Reconnect."""
        from fastapi import HTTPException
        from src.api.routes import sync as sync_routes
        from src.models import SyncLog

        class Gone(Exception):
            body = '{"error_code": "ITEM_NOT_FOUND", "error_message": "gone"}'

        monkeypatch.setattr(sync_routes, "create_link_token",
                            lambda **kw: (_ for _ in ()).throw(Gone()))

        row = SyncLog(source="plaid", sync_type="plaid", status="connected",
                      plaid_item_id="item-dead",
                      extra_data={"institution_name": "Test Bank",
                                  "access_token": "access-prod-dead"})
        db_session.add(row)
        db_session.commit()

        with pytest.raises(HTTPException) as caught:
            sync_routes.get_link_token(account_id=row.id, db=db_session)
        assert caught.value.status_code == 409
        assert "cannot be reconnected" in caught.value.detail
        db_session.refresh(row)
        assert (row.extra_data or {}).get("item_gone") is True

    def test_other_plaid_errors_are_not_mistaken_for_a_dead_item(self, db_session, monkeypatch):
        from fastapi import HTTPException
        from src.api.routes import sync as sync_routes
        from src.models import SyncLog

        class RateLimited(Exception):
            body = '{"error_code": "RATE_LIMIT", "error_message": "slow down"}'

        monkeypatch.setattr(sync_routes, "create_link_token",
                            lambda **kw: (_ for _ in ()).throw(RateLimited()))

        row = SyncLog(source="plaid", sync_type="plaid", status="connected",
                      plaid_item_id="item-busy",
                      extra_data={"institution_name": "Test Bank",
                                  "access_token": "access-prod-busy"})
        db_session.add(row)
        db_session.commit()

        with pytest.raises(HTTPException) as caught:
            sync_routes.get_link_token(account_id=row.id, db=db_session)
        assert caught.value.status_code == 502
        assert "RATE_LIMIT" in caught.value.detail
        db_session.refresh(row)
        assert not (row.extra_data or {}).get("item_gone")
