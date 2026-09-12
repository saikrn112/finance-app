from src.models import SyncLog
from src.services.plaid_health import blocked_error, record_error, record_success
from src.api.routes import sync


def test_successful_transactions_do_not_clear_balance_failure():
    log = SyncLog(extra_data={})
    record_error(log, "balances", {"code": "INTERNAL_SERVER_ERROR"})
    record_success(log, "transactions")
    assert log.extra_data["last_sync_error"]["stage"] == "balances"
    assert blocked_error(log)["action"] == "retry_later"
    record_success(log, "balances")
    assert blocked_error(log) is None
    assert not log.extra_data.get("last_sync_error")


def test_unusable_items_do_not_call_any_plaid_stage(db_session, monkeypatch):
    for code in ["ITEM_NOT_FOUND", "ITEM_LOGIN_REQUIRED"]:
        log = SyncLog(source="plaid", status="connected", extra_data={"institution_name": code, "access_token": "synthetic"})
        record_error(log, "transactions", {"code": code, "message": "Action required"})
        db_session.add(log)
    db_session.commit()
    def unexpected(*args, **kwargs):
        raise AssertionError("Blocked Item must not make provider calls")
    for name in ["sync_transactions", "get_account_balances", "get_investment_holdings", "get_investment_transactions"]:
        monkeypatch.setattr(sync, name, unexpected)
    result = sync._sync_plaid_locked(db_session)
    assert result["status"] == "partial_error"
    assert len(result["errors"]) == 2


def test_removed_item_starts_new_link_without_old_access_token(db_session, monkeypatch):
    log = SyncLog(source="plaid", status="connected", extra_data={"institution_name": "Synthetic Broker", "access_token": "old-token", "item_gone": True})
    db_session.add(log)
    db_session.commit()
    calls = []
    monkeypatch.setattr(sync, "create_link_token", lambda **kw: calls.append(kw) or "new-link")
    assert sync.get_link_token(account_id=log.id, db=db_session)["link_token"] == "new-link"
    assert calls[0]["access_token"] is None
