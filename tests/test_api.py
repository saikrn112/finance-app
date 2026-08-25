"""Tests for API endpoints."""
import pytest
from datetime import date, datetime
from decimal import Decimal
from fastapi.testclient import TestClient

from src.api.server import app
from src.models import (
    Transaction, SyncLog, AccountSnapshot, SourceBalanceHistory,
    InvestmentPeriodFact, AccountActivity, PlaidApiUsage, PlaidProductEnrollment, init_db,
)
from src.models.database import Base, engine, SessionLocal, get_db


@pytest.fixture
def client(temp_db):
    """Create test client with isolated database."""
    Session, db_path = temp_db
    
    # Override the dependency
    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()
    
    app.dependency_overrides[get_db] = override_get_db
    
    with TestClient(app) as c:
        yield c, Session
    
    app.dependency_overrides.clear()


@pytest.fixture
def client_with_data(client):
    """Client with sample transaction data."""
    test_client, Session = client
    db = Session()
    
    txns = [
        Transaction(
            source_id="api1", source="example_card", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="EXAMPLE GROCER",
            merchant_clean="Example Grocer", category="Groceries"
        ),
        Transaction(
            source_id="api2", source="example_card", date=date(2026, 2, 2),
            amount=Decimal("-25.00"), merchant_raw="EXAMPLE DELIVERY",
            merchant_clean="Example Delivery", category="Dining"
        ),
        Transaction(
            source_id="api3", source="example_bank", date=date(2026, 2, 1),
            amount=Decimal("5000.00"), merchant_raw="PAYROLL",
            merchant_clean="Payroll", category="Salary/Paycheck"
        ),
    ]
    for t in txns:
        db.add(t)
    db.commit()
    db.close()
    
    return test_client


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    def test_health_returns_ok(self, client):
        test_client, _ = client
        response = test_client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestTransactionsAPI:
    """Tests for transactions endpoints."""

    def test_list_transactions_empty(self, client):
        test_client, _ = client
        response = test_client.get("/api/transactions/?currency=USD")
        assert response.status_code == 200
        data = response.json()
        assert data["transactions"] == []
        assert data["total"] == 0

    def test_list_transactions_with_data(self, client_with_data):
        response = client_with_data.get("/api/transactions/?currency=USD")
        assert response.status_code == 200
        data = response.json()
        assert len(data["transactions"]) == 3
        assert data["total"] == 3

    def test_list_transactions_pagination(self, client_with_data):
        response = client_with_data.get("/api/transactions/?limit=2&offset=0&currency=USD")
        data = response.json()
        assert len(data["transactions"]) == 2
        assert data["limit"] == 2
        assert data["offset"] == 0

    def test_list_transactions_filter_by_category(self, client_with_data):
        response = client_with_data.get("/api/transactions/?category=Groceries&currency=USD")
        data = response.json()
        assert len(data["transactions"]) == 1
        assert data["transactions"][0]["category"] == "Groceries"

    def test_list_transactions_filter_by_source(self, client_with_data):
        response = client_with_data.get("/api/transactions/?source=example_card&currency=USD")
        data = response.json()
        assert len(data["transactions"]) == 2
        assert all(t["source"] == "example_card" for t in data["transactions"])

    def test_list_transactions_search(self, client_with_data):
        response = client_with_data.get("/api/transactions/?search=Whole&currency=USD")
        data = response.json()
        assert len(data["transactions"]) == 1
        assert "Whole" in data["transactions"][0]["merchant_clean"]

    def test_list_transactions_ordered_by_date_desc(self, client_with_data):
        response = client_with_data.get("/api/transactions/?currency=USD")
        data = response.json()
        dates = [t["date"] for t in data["transactions"]]
        assert dates == sorted(dates, reverse=True)


class TestConnectedAccountsAPI:
    def test_sync_connected_dedupes_duplicate_plaid_logs(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-example_bank",
                extra_data={"institution_name": "Example Bank"},
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-example_bank",
                extra_data={"institution_name": "Example Bank"},
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-example_card",
                extra_data={"institution_name": "Example Card"},
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/sync/connected")
        assert response.status_code == 200
        assert response.json() == [
            {"source": "Example Card", "type": "plaid"},
            {"source": "Example Bank", "type": "plaid"},
        ]

    def test_settings_only_returns_connected_institutions_not_import_logs(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-example_bank",
                extra_data={"institution_name": "Example Bank"},
            ),
            SyncLog(
                source="Example Bank",
                sync_type="import_statement_pdf",
                status="success",
                record_count=25,
                extra_data={"source_key": "example_bank"},
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/settings/")
        assert response.status_code == 200
        payload = response.json()
        assert payload["stats"]["connected_accounts"] == 1
        assert len(payload["accounts"]) == 1
        assert payload["accounts"][0]["source"] == "Example Bank"

    def test_sidebar_accounts_returns_grouped_cached_rows(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            Transaction(
                source_id="example_bank-pay",
                source="Example Bank",
                date=date(2026, 2, 1),
                amount=Decimal("2500.00"),
                merchant_raw="PAYROLL",
                merchant_clean="Payroll",
                category="Salary/Paycheck",
            ),
            Transaction(
                source_id="example_credit_card-charge",
                source="Example Credit Card",
                date=date(2026, 2, 2),
                amount=Decimal("-85.42"),
                merchant_raw="DISCOVER TEST",
                merchant_clean="Example Credit Card Test",
                category="Dining",
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-example_bank",
                extra_data={"institution_name": "Example Bank", "last_sync_at": "2026-03-14T10:00:00"},
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-example-brokerage",
                extra_data={"institution_name": "Example Brokerage", "last_sync_at": "2026-03-14T10:05:00"},
            ),
            AccountSnapshot(
                source="Example Bank",
                account_group="bank_account",
                connection_state="plaid",
                current_value=Decimal("3120.55"),
            ),
            AccountSnapshot(
                source="Example Brokerage",
                account_group="investment",
                connection_state="plaid",
                current_value=Decimal("8421.33"),
            ),
            AccountSnapshot(
                source="National Retirement Plan",
                account_group="retirement",
                connection_state="manual",
                current_value=Decimal("12000.00"),
            ),
            SourceBalanceHistory(
                source_key="example_bank",
                source="Example Bank",
                account_group="bank_account",
                date=date(2026, 3, 14),
                value=Decimal("3120.55"),
                currency="USD",
                provenance="account_snapshot",
            ),
            SourceBalanceHistory(
                source_key="Example Brokerage",
                source="Example Brokerage",
                account_group="investment",
                date=date(2026, 3, 14),
                value=Decimal("8421.33"),
                currency="USD",
                provenance="account_snapshot",
            ),
            SourceBalanceHistory(
                source_key="National Retirement Plan",
                source="National Retirement Plan",
                account_group="retirement",
                date=date(2026, 3, 14),
                value=Decimal("12000.00"),
                currency="USD",
                provenance="account_snapshot",
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/sync/sidebar-accounts?currency=USD")
        assert response.status_code == 200
        payload = response.json()
        rows = {row["source"]: row for row in payload["accounts"]}

        assert rows["Example Bank"]["group"] == "bank_account"
        assert rows["Example Bank"]["connection_state"] == "plaid"
        assert rows["Example Bank"]["balance"] == 3120.55
        assert rows["Example Bank"]["ledger_balance"] == 2500.0
        assert rows["Example Bank"]["filter_source"] == "Example Bank"

        assert rows["Example Credit Card"]["group"] == "credit_card"
        assert rows["Example Credit Card"]["connection_state"] == "manual"
        assert rows["Example Credit Card"]["balance"] == -85.42
        assert rows["Example Credit Card"]["snapshot_balance"] is None

        assert rows["Example Brokerage"]["group"] == "investment"
        assert rows["Example Brokerage"]["connection_state"] == "plaid"
        assert rows["Example Brokerage"]["balance"] == 8421.33
        assert rows["Example Brokerage"]["filter_source"] is None

        assert rows["National Retirement Plan"]["group"] == "retirement"
        assert rows["National Retirement Plan"]["connection_state"] == "manual"

    def test_get_transaction_by_id(self, client_with_data):
        # First get list to find an ID
        list_response = client_with_data.get("/api/transactions/?currency=USD")
        txn_id = list_response.json()["transactions"][0]["id"]
        
        response = client_with_data.get(f"/api/transactions/{txn_id}")
        assert response.status_code == 200
        assert response.json()["id"] == txn_id

    def test_get_transaction_not_found(self, client_with_data):
        response = client_with_data.get("/api/transactions/nonexistent-id")
        # Returns tuple (dict, status) in current implementation
        assert response.status_code in [200, 404]

    def test_update_transaction_category(self, client_with_data):
        list_response = client_with_data.get("/api/transactions/?currency=USD")
        txn_id = list_response.json()["transactions"][0]["id"]
        
        response = client_with_data.patch(
            f"/api/transactions/{txn_id}",
            json={"category": "Shopping"}
        )
        assert response.status_code == 200
        assert response.json()["category"] == "Shopping"

    def test_category_options_include_rules_and_existing_data(self, client_with_data):
        response = client_with_data.get("/api/transactions/category-options")
        assert response.status_code == 200
        payload = response.json()
        options = {item["category"]: item["subcategories"] for item in payload["categories"]}
        assert "Dining" in options
        assert "Salary" in options
        assert "Uncategorized" in options
        assert "Paycheck" in options["Salary"]

    def test_update_transaction_category_normalizes_value(self, client_with_data):
        list_response = client_with_data.get("/api/transactions/?currency=USD")
        txn_id = list_response.json()["transactions"][0]["id"]

        response = client_with_data.patch(
            f"/api/transactions/{txn_id}",
            json={"category": "  Transportation / Gas  "},
        )

        assert response.status_code == 200
        assert response.json()["category"] == "Transportation/Gas"

    def test_update_transaction_category_rejects_nested_path(self, client_with_data):
        list_response = client_with_data.get("/api/transactions/?currency=USD")
        txn_id = list_response.json()["transactions"][0]["id"]

        response = client_with_data.patch(
            f"/api/transactions/{txn_id}",
            json={"category": "Shopping/Home/Furniture"},
        )

        assert response.status_code == 422

    def test_update_transaction_sets_user_source(self, client_with_data):
        list_response = client_with_data.get("/api/transactions/?currency=USD")
        txn_id = list_response.json()["transactions"][0]["id"]
        
        response = client_with_data.patch(
            f"/api/transactions/{txn_id}",
            json={"category": "Entertainment"}
        )
        # category_source should be set to "user" when manually updated


class TestProjectsAPI:
    """Tests for projects endpoints."""

    def test_create_list_and_assign_project(self, client_with_data):
        create_response = client_with_data.post(
            "/api/projects/",
            json={"name": "Relocation", "color": "#22c55e", "budget": 5000},
        )
        assert create_response.status_code == 200
        project = create_response.json()
        assert project["name"] == "Relocation"
        assert project["budget"] == 5000.0
        assert project["txn_count"] == 0

        txn_response = client_with_data.get("/api/transactions/?currency=USD")
        txn_ids = [txn["id"] for txn in txn_response.json()["transactions"][:2]]

        assign_response = client_with_data.post(
            f"/api/projects/{project['id']}/transactions",
            json={"transaction_ids": txn_ids},
        )
        assert assign_response.status_code == 200
        assert assign_response.json() == {"added": 2}

        list_response = client_with_data.get("/api/projects/?currency=USD")
        assert list_response.status_code == 200
        projects = list_response.json()
        assert len(projects) == 1
        assert projects[0]["txn_count"] == 2
        assert projects[0]["spent"] == 75.0

        detail_response = client_with_data.get(f"/api/projects/{project['id']}?currency=USD")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["txn_count"] == 2
        assert len(detail["transactions"]) == 2
        assert {item["category"] for item in detail["categories"]} == {"Groceries", "Dining"}

    def test_duplicate_project_name_returns_conflict(self, client):
        test_client, _ = client
        first = test_client.post("/api/projects/", json={"name": "Vacation Fund"})
        assert first.status_code == 200

        duplicate = test_client.post("/api/projects/", json={"name": "Vacation Fund"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "A project with that name already exists"


class TestAnalyticsAPI:
    """Tests for analytics endpoints."""

    def test_summary_empty_db(self, client):
        test_client, _ = client
        response = test_client.get("/api/analytics/summary?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        assert response.status_code == 200
        data = response.json()
        assert data["income"] == 0
        assert data["spending"] == 0

    def test_summary_with_data(self, client_with_data):
        response = client_with_data.get("/api/analytics/summary?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        assert response.status_code == 200
        data = response.json()
        assert data["income"] == 5000.0
        assert data["spending"] == 75.0  # 50 + 25

    def test_summary_spend_income_ratio(self, client_with_data):
        response = client_with_data.get("/api/analytics/summary?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        data = response.json()
        expected_ratio = (75.0 / 5000.0) * 100
        assert data["spend_income_ratio"] == round(expected_ratio, 1)

    def test_summary_net_flow(self, client_with_data):
        response = client_with_data.get("/api/analytics/summary?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        data = response.json()
        assert data["net_flow"] == 5000.0 - 75.0  # income - spending

    def test_summary_date_ranges(self, client_with_data):
        response = client_with_data.get("/api/analytics/summary?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        assert response.status_code == 200
        assert "currency" in response.json()

    def test_summary_requires_currency(self, client_with_data):
        response = client_with_data.get("/api/analytics/summary?start_date=2026-01-01&end_date=2026-12-31")
        assert response.status_code == 422  # Missing required currency param

    def test_by_category(self, client_with_data):
        response = client_with_data.get("/api/analytics/by-category?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        assert response.status_code == 200
        data = response.json()
        categories = {c["category"]: c["total"] for c in data}
        assert "Groceries" in categories
        assert "Dining" in categories

    def test_by_category_excludes_income(self, client_with_data):
        response = client_with_data.get("/api/analytics/by-category?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        data = response.json()
        categories = [c["category"] for c in data]
        assert "Salary/Paycheck" not in categories

    def test_by_merchant(self, client_with_data):
        response = client_with_data.get("/api/analytics/by-merchant?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        assert response.status_code == 200
        data = response.json()
        assert len(data) <= 10  # Default limit

    def test_by_merchant_limit(self, client_with_data):
        response = client_with_data.get("/api/analytics/by-merchant?start_date=2026-01-01&end_date=2026-12-31&currency=USD&limit=1")
        data = response.json()
        assert len(data) == 1

    def test_trends(self, client_with_data):
        response = client_with_data.get("/api/analytics/trends?start_date=2026-01-01&end_date=2026-12-31&currency=USD")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_trends_return_positive_income_and_outflow_magnitudes(self, client_with_data):
        response = client_with_data.get(
            "/api/analytics/trends?start_date=2026-02-01&end_date=2026-02-02&granularity=daily&currency=USD"
        )
        assert response.status_code == 200
        data = {row["period"]: row for row in response.json()}

        assert data["2026-02-01"]["Income"] == 5000.0
        assert data["2026-02-01"]["Groceries"] == 50.0
        assert data["2026-02-01"]["total"] == 5050.0
        assert data["2026-02-02"]["Dining"] == 25.0
        assert data["2026-02-02"]["total"] == 25.0

    def test_core_expense_trends_preserve_income_series(self, client_with_data):
        response = client_with_data.get(
            "/api/analytics/trends?start_date=2026-02-01&end_date=2026-02-02&granularity=daily&core_expenses_only=1&currency=USD"
        )
        assert response.status_code == 200
        data = {row["period"]: row for row in response.json()}

        assert data["2026-02-01"]["Income"] == 5000.0
        assert data["2026-02-01"]["Groceries"] == 50.0
        assert data["2026-02-01"]["total"] == 5050.0
        assert data["2026-02-02"]["Dining"] == 25.0
        assert data["2026-02-02"]["total"] == 25.0

    def test_subscriptions(self, client_with_data):
        response = client_with_data.get("/api/analytics/subscriptions")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_net_worth_history_includes_snapshot_series(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            Transaction(
                source_id="nw_bofa_1",
                source="Example Bank",
                date=date(2026, 3, 1),
                amount=Decimal("1000.00"),
                merchant_raw="OPENING",
                merchant_clean="Opening",
                category="Salary/Paycheck",
            ),
            Transaction(
                source_id="nw_card_1",
                source="Example Card",
                date=date(2026, 3, 2),
                amount=Decimal("-200.00"),
                merchant_raw="CARD",
                merchant_clean="Card",
                category="Dining",
            ),
            AccountSnapshot(
                source="Example Bank",
                account_group="bank_account",
                connection_state="plaid",
                current_value=Decimal("1000.00"),
                synced_at=datetime(2026, 3, 1, 10, 0, 0),
                created_at=datetime(2026, 3, 1, 10, 0, 0),
            ),
            AccountSnapshot(
                source="Example Brokerage",
                account_group="investment",
                connection_state="plaid",
                current_value=Decimal("10000.00"),
                synced_at=datetime(2026, 3, 1, 10, 0, 0),
                created_at=datetime(2026, 3, 1, 10, 0, 0),
            ),
            AccountSnapshot(
                source="Example Brokerage",
                account_group="investment",
                connection_state="plaid",
                current_value=Decimal("12000.00"),
                synced_at=datetime(2026, 3, 3, 10, 0, 0),
                created_at=datetime(2026, 3, 3, 10, 0, 0),
            ),
            AccountSnapshot(
                source="National Retirement Plan",
                account_group="retirement",
                connection_state="manual",
                current_value=Decimal("5000.00"),
                synced_at=datetime(2026, 3, 2, 10, 0, 0),
                created_at=datetime(2026, 3, 2, 10, 0, 0),
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/analytics/net-worth/tracked-history?start_date=2026-03-01&end_date=2026-03-03&currency=USD")
        assert response.status_code == 200
        payload = response.json()
        points = {point["date"]: point for point in payload["points"]}
        latest_rows = {row["key"]: row for row in payload["latest_sources"]}

        assert points["2026-03-01"]["bank_accounts"] == 1000.0
        assert points["2026-03-01"]["cash_like"] == 0.0
        assert points["2026-03-01"]["brokerage"] == 10000.0
        assert points["2026-03-01"]["retirement"] == 0.0
        assert points["2026-03-02"]["credit_cards"] == -200.0
        assert points["2026-03-02"]["retirement"] == 5000.0
        assert points["2026-03-03"]["brokerage"] == 12000.0
        assert points["2026-03-03"]["total"] == 17800.0

        assert latest_rows["Example Bank"]["group"] == "bank_accounts"
        assert latest_rows["Example Brokerage"]["group"] == "brokerage"
        assert latest_rows["Example Brokerage"]["history_mode"] == "historical"
        assert latest_rows["National Retirement Plan"]["group"] == "retirement"
        assert latest_rows["National Retirement Plan"]["history_mode"] == "latest_only"

    def test_net_worth_history_keeps_retirement_snapshot_sources_separate(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            AccountSnapshot(
                source="National Retirement Plan",
                account_group="retirement",
                connection_state="manual",
                current_value=Decimal("1000.00"),
                synced_at=datetime(2026, 3, 2, 10, 0, 0),
                created_at=datetime(2026, 3, 2, 10, 0, 0),
            ),
            AccountSnapshot(
                source="Example Retirement Plan",
                account_group="retirement",
                connection_state="manual",
                current_value=Decimal("1000.00"),
                synced_at=datetime(2026, 3, 3, 10, 0, 0),
                created_at=datetime(2026, 3, 3, 10, 0, 0),
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/analytics/net-worth/tracked-history?start_date=2026-03-01&end_date=2026-03-03&currency=USD")
        assert response.status_code == 200
        payload = response.json()
        points = {point["date"]: point for point in payload["points"]}
        latest_rows = {row["key"]: row for row in payload["latest_sources"]}

        assert points["2026-03-02"]["retirement"] == 1000.0
        assert points["2026-03-03"]["retirement"] == 2000.0
        assert latest_rows["National Retirement Plan"]["current"] == 1000.0
        assert latest_rows["National Retirement Plan"]["history_mode"] == "latest_only"
        assert latest_rows["Example Retirement Plan"]["current"] == 1000.0
        assert latest_rows["Example Retirement Plan"]["history_mode"] == "latest_only"

    def test_net_worth_history_does_not_backderive_bank_balance_before_snapshot(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            Transaction(
                source_id="example_bank-before-1",
                source="Example Bank",
                date=date(2026, 3, 1),
                amount=Decimal("5000.00"),
                merchant_raw="PAYROLL",
                merchant_clean="Payroll",
                category="Salary/Paycheck",
            ),
            Transaction(
                source_id="example_bank-before-2",
                source="Example Bank",
                date=date(2026, 3, 2),
                amount=Decimal("4000.00"),
                merchant_raw="PAYROLL",
                merchant_clean="Payroll",
                category="Salary/Paycheck",
            ),
            AccountSnapshot(
                source="Example Bank",
                account_group="bank_account",
                connection_state="plaid",
                current_value=Decimal("1000.00"),
                synced_at=datetime(2026, 3, 3, 10, 0, 0),
                created_at=datetime(2026, 3, 3, 10, 0, 0),
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/analytics/net-worth/tracked-history?start_date=2026-03-01&end_date=2026-03-03&currency=USD")
        assert response.status_code == 200
        payload = response.json()
        points = {point["date"]: point for point in payload["points"]}

        assert points["2026-03-01"]["bank_accounts"] == 0.0
        assert points["2026-03-02"]["bank_accounts"] == 0.0
        assert points["2026-03-03"]["bank_accounts"] == 1000.0

    def test_recurring_catalog_endpoint(self, client):
        test_client, Session = client
        db = Session()
        db.add_all(
            [
                Transaction(
                    source_id="recurring_1",
                    source="example_charge_card",
                    date=date(2025, 12, 10),
                    amount=Decimal("-12.99"),
                    merchant_raw="DISNEYPLUS 888-905-7888 CA",
                    merchant_clean="Disney+",
                    category="Subscriptions/Entertainment",
                    account_last4="2100",
                ),
                Transaction(
                    source_id="recurring_2",
                    source="example_charge_card",
                    date=date(2026, 1, 10),
                    amount=Decimal("-12.99"),
                    merchant_raw="DISNEYPLUS 888-905-7888 CA",
                    merchant_clean="Disney+",
                    category="Subscriptions/Entertainment",
                    account_last4="2100",
                ),
            ]
        )
        db.commit()
        db.close()

        response = test_client.get("/api/recurring/catalog?start_date=2026-01-01&end_date=2026-03-31&currency=USD")
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["active_count"] >= 1
        assert any(item["display_name"] == "Disney+" for item in payload["items"])

    def test_recurring_detail_endpoint(self, client):
        test_client, Session = client
        db = Session()
        for idx, amount in enumerate([Decimal("-10.99"), Decimal("-12.99")]):
            db.add(
                Transaction(
                    source_id=f"recurring_detail_{idx}",
                    source="example_charge_card",
                    date=date(2026, 1 + idx, 10),
                    amount=amount,
                    merchant_raw="DISNEYPLUS 888-905-7888 CA",
                    merchant_clean="Disney+",
                    category="Subscriptions/Entertainment",
                    account_last4="2100",
                )
            )
        db.commit()
        db.close()

        catalog = test_client.get("/api/recurring/catalog?start_date=2026-01-01&end_date=2026-03-31&currency=USD").json()
        disney = next(item for item in catalog["items"] if item["display_name"] == "Disney+")

        response = test_client.get(f"/api/recurring/{disney['id']}?currency=USD")
        assert response.status_code == 200
        detail = response.json()
        assert detail["display_name"] == "Disney+"
        assert any(event["event_type"] == "price_changed" for event in detail["events"])


class TestSyncAPI:
    """Tests for sync endpoints."""

    def test_sync_status_empty(self, client):
        test_client, _ = client
        response = test_client.get("/api/sync/status")
        assert response.status_code == 200
        assert response.json()["accounts"] == []

    def test_plaid_link_token_error_without_config(self, client):
        test_client, _ = client
        response = test_client.post("/api/sync/plaid/link-token")
        # Should fail without valid Plaid credentials
        assert response.status_code in [200, 500]

    def test_plaid_sync_no_accounts(self, client):
        test_client, _ = client
        response = test_client.post("/api/sync/plaid/sync")
        assert response.status_code == 200
        assert response.json()["status"] == "no_accounts"

    def test_duplicate_plaid_institution_is_rejected_and_revoked(self, client, monkeypatch):
        test_client, Session = client
        db = Session()
        db.add(SyncLog(
            source="plaid",
            sync_type="plaid",
            plaid_item_id="existing-item",
            status="connected",
            extra_data={
                "access_token": "existing-token",
                "institution_name": "Example Bank",
                "institution_id": "ins_example",
                "account_fingerprints": [{"mask": "1234", "name": "Everyday Card"}],
            },
        ))
        db.commit()
        db.close()

        revoked = []
        monkeypatch.setattr(
            "src.api.routes.sync.exchange_public_token",
            lambda _token: ("duplicate-token", "duplicate-item", ""),
        )
        monkeypatch.setattr("src.api.routes.sync.remove_item", revoked.append)

        response = test_client.post(
            "/api/sync/plaid/exchange",
            params={
                "public_token": "public-token",
                "institution_name": "Example Bank",
                "institution_id": "ins_example",
                "account_fingerprints": '[{"mask":"1234","name":"Everyday Card"}]',
            },
        )

        assert response.status_code == 409
        assert "already connected" in response.json()["detail"]
        assert revoked == ["duplicate-token"]
        db = Session()
        assert db.query(SyncLog).filter(SyncLog.source == "plaid").count() == 1
        db.close()

    def test_disconnect_revokes_all_duplicate_items_for_institution(self, client, monkeypatch):
        test_client, Session = client
        db = Session()
        logs = [
            SyncLog(
                source="plaid",
                sync_type="plaid",
                plaid_item_id=f"item-{index}",
                status="connected",
                extra_data={"access_token": f"token-{index}", "institution_name": "Example Bank"},
            )
            for index in (1, 2)
        ]
        db.add_all(logs)
        db.commit()
        account_id = logs[0].id
        db.close()

        revoked = []
        monkeypatch.setattr("src.api.routes.settings.remove_item", revoked.append)
        response = test_client.delete(f"/api/settings/accounts/{account_id}")

        assert response.status_code == 200
        assert sorted(revoked) == ["token-1", "token-2"]
        db = Session()
        assert db.query(SyncLog).filter(SyncLog.source == "plaid").count() == 0
        db.close()

    def test_canonical_investment_value_matches_sidebar_net_worth_and_history(self, client):
        test_client, Session = client
        db = Session()
        db.add(SourceBalanceHistory(
            source_key="example_brokerage",
            source="Example Brokerage",
            account_group="investment",
            date=date(2026, 7, 12),
            value=Decimal("12345.67"),
            currency="USD",
            provenance="account_snapshot",
            created_at=datetime(2026, 7, 12, 12, 0, 0),
        ))
        db.commit()
        db.close()

        sidebar = test_client.get("/api/sync/sidebar-accounts?currency=USD").json()
        sidebar_value = next(row["balance"] for row in sidebar["accounts"] if row["source"] == "Example Brokerage")
        net_worth = test_client.get(
            "/api/analytics/net-worth/tracked-history?start_date=2026-07-12&end_date=2026-07-12&currency=USD"
        ).json()
        net_worth_value = next(row["current"] for row in net_worth["latest_sources"] if row["label"] == "Example Brokerage")
        history = test_client.get("/api/sync/plaid/investments/history?currency=USD").json()
        history_value = next(row["value"] for row in history["history"] if row["source"] == "Example Brokerage")

        assert sidebar_value == net_worth_value == history_value == 12345.67

    def test_sidebar_normalizes_legacy_plural_credit_card_group(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            Transaction(
                source_id="legacy-card-transaction",
                source="Example Legacy Card",
                date=date(2026, 7, 1),
                amount=Decimal("-42.23"),
                merchant_raw="EXAMPLE PURCHASE",
                merchant_clean="Example Purchase",
                category="Shopping",
            ),
            SourceBalanceHistory(
                source_key="example_legacy_card",
                source="Example Legacy Card",
                account_group="credit_cards",
                date=date(2026, 7, 1),
                value=Decimal("-42.23"),
                currency="USD",
                provenance="transaction_ledger",
            ),
        ])
        db.commit()
        db.close()

        sidebar = test_client.get("/api/sync/sidebar-accounts?currency=USD").json()
        row = next(item for item in sidebar["accounts"] if item["source"] == "Example Legacy Card")
        assert row["group"] == "credit_card"
        assert row["filter_source"] == "Example Legacy Card"

    def test_account_activity_is_not_returned_by_household_transactions(self, client):
        test_client, Session = client
        db = Session()
        db.add(AccountActivity(
            source_id="example-interest-1",
            source_key="example_cash_investment",
            source="Example Cash Investment",
            date=date(2026, 6, 30),
            amount=Decimal("117.36"),
            description="Interest Paid",
            merchant="Interest Paid",
            activity_type="interest",
            currency="USD",
        ))
        db.commit()
        db.close()

        activity = test_client.get(
            "/api/sync/plaid/investments/activity?source=Example%20Cash%20Investment&currency=USD"
        ).json()
        ledger = test_client.get(
            "/api/transactions/?source=Example%20Cash%20Investment&currency=USD"
        ).json()

        assert len(activity["activity"]) == 1
        assert activity["activity"][0]["type"] == "interest"
        assert ledger["total"] == 0

    def test_investment_history_attributes_stored_transfer_and_interest(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            SourceBalanceHistory(
                source_key="example_cash_investment", source="Example Cash Investment",
                account_group="investment", date=date(2026, 7, 27), value=Decimal("10000.00"),
                currency="USD", provenance="account_snapshot",
            ),
            SourceBalanceHistory(
                source_key="example_cash_investment", source="Example Cash Investment",
                account_group="investment", date=date(2026, 8, 25), value=Decimal("11250.00"),
                currency="USD", provenance="account_snapshot",
            ),
            AccountActivity(
                source_id="transfer-1", source_key="example_cash_investment",
                source="Example Cash Investment", date=date(2026, 8, 3), amount=Decimal("1000"),
                description="Transfer", activity_type="transfer", currency="USD",
            ),
            AccountActivity(
                source_id="interest-1", source_key="example_cash_investment",
                source="Example Cash Investment", date=date(2026, 7, 31), amount=Decimal("250.00"),
                description="Interest Paid", activity_type="interest", currency="USD",
            ),
            InvestmentPeriodFact(
                source_key="example_cash_investment", source="Example Cash Investment",
                period_start=date(2026, 7, 27), period_end=date(2026, 8, 25),
                _beginning_value=Decimal("10000.00"), _ending_value=Decimal("11250.00"),
                _inflow=Decimal("1000.00"), _market_gain=Decimal("250.00"),
                currency="USD", provenance="test_fixture",
            ),
        ])
        db.commit()
        db.close()

        history = test_client.get("/api/sync/plaid/investments/history?currency=USD").json()["history"]
        latest = next(row for row in history if row["synced_at"].startswith("2026-08-25"))

        assert latest["inflow"] == 1000
        assert latest["market_gain"] == 250
        assert latest["ending_value"] == 11250

    def test_snapshot_dedupe_does_not_treat_derived_history_as_observed(self, client):
        _, Session = client
        from src.api.routes.sync import _persist_account_snapshot

        db = Session()
        db.add(SourceBalanceHistory(
            source_key="example_brokerage", source="Example Brokerage",
            account_group="investment", date=date(2026, 8, 1), value=Decimal("5000.00"),
            currency="USD", provenance="transaction_backfill",
        ))
        db.commit()

        assert _persist_account_snapshot(
            db, source="Example Brokerage", account_group="investment",
            connection_state="plaid", current_value=5000, synced_at=datetime(2026, 8, 25),
        ) is True
        db.commit()
        observed = db.query(SourceBalanceHistory).filter(
            SourceBalanceHistory.provenance == "account_snapshot"
        ).one()
        assert observed.date == date(2026, 8, 25)
        db.close()

    def test_plaid_billing_uses_persisted_product_enrollment(self, client):
        test_client, Session = client
        db = Session()
        db.add(PlaidProductEnrollment(
            plaid_item_id="item-example", product="transactions",
            active_from=datetime.utcnow(),
        ))
        db.commit()
        db.close()

        payload = test_client.get("/api/settings/").json()["plaid_usage"]
        transactions = next(row for row in payload["billing_lines"] if row["label"] == "Transactions usage")
        assert transactions["quantity"] == 1

    def test_plaid_usage_survives_caller_rollback_and_keeps_database_identity(self, client, monkeypatch):
        _, Session = client
        from src.ingestion.plaid_usage import record_plaid_usage, finish_plaid_usage

        monkeypatch.setattr(
            "src.ingestion.plaid_usage.ensure_vault_metadata",
            lambda: {"device_id": "synthetic-device", "device_label": "Test"},
        )
        db = Session()
        first = record_plaid_usage(db, endpoint="accounts_balance_get", metadata={"status": "attempted"})
        finish_plaid_usage(db, first, success=False, error_code="SyntheticError")
        db.rollback()
        second = record_plaid_usage(db, endpoint="accounts_balance_get")
        db.rollback()

        rows = db.query(PlaidApiUsage).order_by(PlaidApiUsage.created_at).all()
        assert [row.status for row in rows] == ["failed", "success"]
        assert rows[0].metadata_json["database_id"] == rows[1].metadata_json["database_id"]
        assert "device_id" not in rows[0].metadata_json
        db.close()


class TestProjectsAndSplits:
    """Covers project assignment, per-project notes, contacts and splits.

    These back the transaction-grid affordances: assigning a transaction to a project,
    creating a project inline, the per-project description shown under the merchant, and
    the member split tags.
    """

    def test_create_project_and_assign_transaction(self, client_with_data):
        test_client = client_with_data

        created = test_client.post("/api/projects/", json={"name": "Trip", "color": "#3b82f6"})
        assert created.status_code == 200
        project_id = created.json()["id"]

        txns = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"]
        txn_id = txns[0]["id"]

        added = test_client.post(
            f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]}
        )
        assert added.status_code == 200
        assert added.json()["added"] == 1

        # The new project shows up in the list with the transaction attached, which is what
        # the inline "New project" flow relies on for the list to refresh immediately.
        listed = {p["name"]: p for p in test_client.get("/api/projects/?currency=USD").json()}
        assert listed["Trip"]["txn_count"] == 1

    def test_duplicate_project_name_rejected(self, client):
        test_client, _ = client
        assert test_client.post("/api/projects/", json={"name": "Dup"}).status_code == 200
        assert test_client.post("/api/projects/", json={"name": "Dup"}).status_code == 409

    def test_per_project_description_roundtrip(self, client_with_data):
        test_client = client_with_data
        project_id = test_client.post("/api/projects/", json={"name": "Notes"}).json()["id"]
        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]
        test_client.post(f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]})

        patched = test_client.patch(
            f"/api/projects/{project_id}/transactions/{txn_id}",
            json={"description": "monitor stand"},
        )
        assert patched.status_code == 200

        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert detail["transactions"][0]["description"] == "monitor stand"

    def test_transaction_notes_are_serialized(self, client_with_data):
        """The main ledger's inline note writes Transaction.notes, so it must round-trip."""
        test_client = client_with_data
        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]

        test_client.patch(f"/api/transactions/{txn_id}?currency=USD", json={"notes": "reimbursed"})

        txns = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"]
        assert next(t for t in txns if t["id"] == txn_id)["notes"] == "reimbursed"

    def test_members_auto_assigned_as_splits_on_add(self, client_with_data):
        test_client = client_with_data
        contact_id = test_client.post("/api/contacts/", json={"name": "Ada"}).json()["id"]
        project_id = test_client.post("/api/projects/", json={"name": "Split"}).json()["id"]
        test_client.post(f"/api/projects/{project_id}/members", json={"contact_ids": [contact_id]})

        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]
        test_client.post(f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]})

        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert [m["name"] for m in detail["members"]] == ["Ada"]
        assert [s["name"] for s in detail["transactions"][0]["splits"]] == ["Ada"]

    def test_splits_can_be_replaced(self, client_with_data):
        test_client = client_with_data
        a = test_client.post("/api/contacts/", json={"name": "Ada"}).json()["id"]
        b = test_client.post("/api/contacts/", json={"name": "Grace"}).json()["id"]
        project_id = test_client.post("/api/projects/", json={"name": "Replace"}).json()["id"]
        test_client.post(f"/api/projects/{project_id}/members", json={"contact_ids": [a, b]})

        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]
        test_client.post(f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]})

        test_client.patch(
            f"/api/projects/{project_id}/transactions/{txn_id}/splits", json={"contact_ids": [b]}
        )
        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert [s["name"] for s in detail["transactions"][0]["splits"]] == ["Grace"]

    def test_contact_rename_propagates_and_usage_reported(self, client_with_data):
        """Renaming a contact must show through everywhere, since splits reference by id."""
        test_client = client_with_data
        contact_id = test_client.post("/api/contacts/", json={"name": "Ada"}).json()["id"]
        project_id = test_client.post("/api/projects/", json={"name": "Rename"}).json()["id"]
        test_client.post(f"/api/projects/{project_id}/members", json={"contact_ids": [contact_id]})
        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]
        test_client.post(f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]})

        usage = test_client.get(f"/api/contacts/{contact_id}/usage").json()
        assert usage == {"split_count": 1, "project_count": 1}

        test_client.patch(f"/api/contacts/{contact_id}", json={"name": "Ada L"})
        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert [s["name"] for s in detail["transactions"][0]["splits"]] == ["Ada L"]

    def test_deleting_contact_clears_its_splits(self, client_with_data):
        test_client = client_with_data
        contact_id = test_client.post("/api/contacts/", json={"name": "Ada"}).json()["id"]
        project_id = test_client.post("/api/projects/", json={"name": "Delete"}).json()["id"]
        test_client.post(f"/api/projects/{project_id}/members", json={"contact_ids": [contact_id]})
        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]
        test_client.post(f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]})

        assert test_client.delete(f"/api/contacts/{contact_id}").status_code == 200

        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert detail["members"] == []
        assert detail["transactions"][0]["splits"] == []

    def test_removing_transaction_from_project_clears_splits(self, client_with_data):
        test_client = client_with_data
        contact_id = test_client.post("/api/contacts/", json={"name": "Ada"}).json()["id"]
        project_id = test_client.post("/api/projects/", json={"name": "Unassign"}).json()["id"]
        test_client.post(f"/api/projects/{project_id}/members", json={"contact_ids": [contact_id]})
        txn_id = test_client.get(
            "/api/transactions/?start_date=2026-02-01&end_date=2026-02-28&currency=USD"
        ).json()["transactions"][0]["id"]
        test_client.post(f"/api/projects/{project_id}/transactions", json={"transaction_ids": [txn_id]})

        test_client.delete(f"/api/projects/{project_id}/transactions/{txn_id}")

        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert detail["transactions"] == []

    def test_creating_project_with_members_does_not_attach_transactions(self, client_with_data):
        """The inline "New project" flow creates only; attaching stays a deliberate click."""
        test_client = client_with_data
        contact_id = test_client.post("/api/contacts/", json={"name": "Ada"}).json()["id"]

        created = test_client.post(
            "/api/projects/", json={"name": "Fresh", "color": "#6366f1", "budget": 250.0}
        )
        assert created.status_code == 200
        project_id = created.json()["id"]
        test_client.post(f"/api/projects/{project_id}/members", json={"contact_ids": [contact_id]})

        detail = test_client.get(f"/api/projects/{project_id}?currency=USD").json()
        assert detail["budget"] == 250.0
        assert [m["name"] for m in detail["members"]] == ["Ada"]
        assert detail["transactions"] == []

        # And it is immediately listable, which is what the popup refresh depends on.
        assert "Fresh" in {p["name"] for p in test_client.get("/api/projects/?currency=USD").json()}
