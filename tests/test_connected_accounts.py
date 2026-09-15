from datetime import date, datetime

from src.models import AccountActivity, ConnectedAccount, ExchangeRate, SourceBalanceHistory, SyncLog, Transaction
from src.api.routes import sync, analytics
from src.ingestion.plaid_activity import relink_orphaned_account_rows
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


def test_provider_activity_is_scoped_to_each_connected_account(db_session, monkeypatch):
    db = db_session
    monkeypatch.setattr(sync, "ensure_rates_fresh", lambda db: None)
    log = SyncLog(source="plaid", status="connected", extra_data={"institution_name": "Test Brokerage"})
    db.add(log)
    db.flush()
    accounts = sync._upsert_connected_accounts(db, log, "Test Brokerage", [
        {"account_id": f"synthetic-{number}", "name": f"Account {number}", "type": "investment"}
        for number in range(3)
    ], datetime(2026, 1, 2))
    for number in range(3):
        db.add(AccountActivity(
            source="Test Brokerage", source_id=f"activity-{number}",
            account_id=f"synthetic-{number}", date=date(2026, 1, 2),
            amount=number + 1, description=f"Trade {number}", currency="USD",
        ))
    db.commit()

    for number in range(3):
        result = sync.get_investment_activity(
            source=accounts[f"synthetic-{number}"].id, db=db, currency="USD",
        )
        assert [row["description"] for row in result["activity"]] == [f"Trade {number}"]


def test_reconnect_relinks_historical_rows_only_for_unique_masks(db_session, monkeypatch):
    db = db_session
    monkeypatch.setattr(sync, "ensure_rates_fresh", lambda db: None)
    log = SyncLog(source="plaid", status="connected", extra_data={"institution_name": "Test Brokerage"})
    db.add(log)
    db.flush()
    accounts = sync._upsert_connected_accounts(db, log, "Test Brokerage", [
        {"account_id": "new-first", "name": "First", "mask": "1001", "type": "investment"},
        {"account_id": "new-second", "name": "Second", "mask": "1002", "type": "investment"},
    ], datetime(2026, 1, 2))
    db.add(AccountActivity(source="Test Brokerage", source_id="legacy-trade", account_id="old-second",
                           account_last4="1002", date=date(2026, 1, 1), amount=12,
                           description="Legacy trade", currency="USD"))
    db.add(Transaction(source="Test Brokerage", source_id="legacy-transfer", plaid_account_id="old-second",
                       account_last4="1002", date=date(2026, 1, 1), amount=5,
                       merchant_raw="Legacy transfer", currency="USD"))
    db.commit()

    assert relink_orphaned_account_rows(db, "Test Brokerage") == (1, 1)
    db.commit()
    first = sync.get_investment_activity(source=accounts["new-first"].id, db=db, currency="USD")
    second = sync.get_investment_activity(source=accounts["new-second"].id, db=db, currency="USD")
    assert first["activity"] == []
    assert {row["description"] for row in second["activity"]} == {"Legacy trade", "Legacy transfer"}
    assert relink_orphaned_account_rows(db, "Test Brokerage") == (0, 0)
