from datetime import date, datetime

from src.models import ConnectedAccount, ExchangeRate, SourceBalanceHistory, SyncLog, Transaction
from src.api.routes import sync, analytics
from src.services.account_filters import transaction_account_filter


def test_two_accounts_keep_balances_history_and_activity_separate(db_session, monkeypatch):
    db = db_session
    monkeypatch.setattr(sync, "ensure_rates_fresh", lambda db: None)
    monkeypatch.setattr(analytics, "ensure_rates_fresh", lambda db: None)
    log = SyncLog(source="plaid", status="connected", extra_data={"institution_name": "Test Institution"})
    db.add(log)
    db.flush()
    accounts = sync._upsert_connected_accounts(db, log, "Test Institution", [
        {"account_id": "synthetic-saving", "name": "Savings", "type": "depository", "subtype": "savings"},
        {"account_id": "synthetic-cd", "name": "CD", "type": "depository", "subtype": "cd"},
    ], datetime(2026, 1, 2))
    db.add(SourceBalanceHistory(source="Test Institution", source_key="Test Institution", account_group="bank_account",
                               date=date(2026, 1, 1), value=100, currency="USD", provenance="account_snapshot"))
    for external_id, value in [("synthetic-saving", 30), ("synthetic-cd", 70)]:
        account = accounts[external_id]
        sync._persist_account_snapshot(db, source=account.source, account_group=account.account_group,
                                       connection_state="plaid", current_value=value, synced_at=datetime(2026, 1, 2),
                                       account_key=account.id, account_name=account.display_name)
        db.add(Transaction(source="Test Institution", source_id=external_id, plaid_account_id=external_id,
                           date=date(2026, 1, 2), amount=value, merchant_raw="Synthetic transfer", currency="USD"))
    db.commit()
    sidebar = sync._build_sidebar_accounts(db, "USD")
    assert {row["source"]: row["balance"] for row in sidebar} == {"Savings": 30, "CD": 70}
    cd = accounts["synthetic-cd"]
    assert db.query(Transaction).filter(transaction_account_filter([f"account:{cd.id}"])).one().source_id == "synthetic-cd"
    activity = sync.get_investment_activity(source=cd.id, db=db, currency="USD")
    assert [row["amount"] for row in activity["activity"]] == [70]
    assert db.query(ConnectedAccount).count() == 2
    history = analytics._compute_net_worth(db, start_date="2026-01-01", end_date="2026-01-03", target_currency="USD")
    assert [point["total"] for point in history["points"]] == [100, 100, 100]
    assert {row["label"] for row in history["latest_sources"]} == {"Savings", "CD"}
