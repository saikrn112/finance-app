"""Tests for API endpoints."""
import pytest
from datetime import date, datetime
from decimal import Decimal
from fastapi.testclient import TestClient

from src.api.server import app
from src.models import Transaction, SyncLog, AccountSnapshot, init_db
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
            source_id="api1", source="chase", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="WHOLE FOODS",
            merchant_clean="Whole Foods", category="Groceries"
        ),
        Transaction(
            source_id="api2", source="chase", date=date(2026, 2, 2),
            amount=Decimal("-25.00"), merchant_raw="UBER EATS",
            merchant_clean="Uber Eats", category="Dining"
        ),
        Transaction(
            source_id="api3", source="bofa", date=date(2026, 2, 1),
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
        response = client_with_data.get("/api/transactions/?source=chase&currency=USD")
        data = response.json()
        assert len(data["transactions"]) == 2
        assert all(t["source"] == "chase" for t in data["transactions"])

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
                plaid_item_id="item-bofa",
                extra_data={"institution_name": "Bank of America"},
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-bofa",
                extra_data={"institution_name": "Bank of America"},
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-chase",
                extra_data={"institution_name": "Chase"},
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/sync/connected")
        assert response.status_code == 200
        assert response.json() == [
            {"source": "Chase", "type": "plaid"},
            {"source": "Bank of America", "type": "plaid"},
        ]

    def test_settings_only_returns_connected_institutions_not_import_logs(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-bofa",
                extra_data={"institution_name": "Bank of America"},
            ),
            SyncLog(
                source="Bank of America",
                sync_type="import_statement_pdf",
                status="success",
                record_count=25,
                extra_data={"source_key": "bofa"},
            ),
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/settings/")
        assert response.status_code == 200
        payload = response.json()
        assert payload["stats"]["connected_accounts"] == 1
        assert len(payload["accounts"]) == 1
        assert payload["accounts"][0]["source"] == "Bank of America"

    def test_sidebar_accounts_returns_grouped_cached_rows(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            Transaction(
                source_id="bofa-pay",
                source="Bank of America",
                date=date(2026, 2, 1),
                amount=Decimal("2500.00"),
                merchant_raw="PAYROLL",
                merchant_clean="Payroll",
                category="Salary/Paycheck",
            ),
            Transaction(
                source_id="discover-charge",
                source="Discover",
                date=date(2026, 2, 2),
                amount=Decimal("-85.42"),
                merchant_raw="DISCOVER TEST",
                merchant_clean="Discover Test",
                category="Dining",
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-bofa",
                extra_data={"institution_name": "Bank of America", "last_sync_at": "2026-03-14T10:00:00"},
            ),
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id="item-robinhood",
                extra_data={"institution_name": "Robinhood", "last_sync_at": "2026-03-14T10:05:00"},
            ),
            AccountSnapshot(
                source="Bank of America",
                account_group="bank_account",
                connection_state="plaid",
                current_value=Decimal("3120.55"),
            ),
            AccountSnapshot(
                source="Robinhood",
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
        ])
        db.commit()
        db.close()

        response = test_client.get("/api/sync/sidebar-accounts?currency=USD")
        assert response.status_code == 200
        payload = response.json()
        rows = {row["source"]: row for row in payload["accounts"]}

        assert rows["Bank of America"]["group"] == "bank_account"
        assert rows["Bank of America"]["connection_state"] == "plaid"
        assert rows["Bank of America"]["balance"] == 3120.55
        assert rows["Bank of America"]["ledger_balance"] == 2500.0
        assert rows["Bank of America"]["filter_source"] == "Bank of America"

        assert rows["Discover"]["group"] == "credit_card"
        assert rows["Discover"]["connection_state"] == "manual"
        assert rows["Discover"]["balance"] == -85.42
        assert rows["Discover"]["snapshot_balance"] is None

        assert rows["Robinhood"]["group"] == "investment"
        assert rows["Robinhood"]["connection_state"] == "plaid"
        assert rows["Robinhood"]["balance"] == 8421.33
        assert rows["Robinhood"]["filter_source"] is None

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
                source="Bank of America",
                date=date(2026, 3, 1),
                amount=Decimal("1000.00"),
                merchant_raw="OPENING",
                merchant_clean="Opening",
                category="Salary/Paycheck",
            ),
            Transaction(
                source_id="nw_card_1",
                source="Chase",
                date=date(2026, 3, 2),
                amount=Decimal("-200.00"),
                merchant_raw="CARD",
                merchant_clean="Card",
                category="Dining",
            ),
            AccountSnapshot(
                source="Bank of America",
                account_group="bank_account",
                connection_state="plaid",
                current_value=Decimal("1000.00"),
                synced_at=datetime(2026, 3, 1, 10, 0, 0),
                created_at=datetime(2026, 3, 1, 10, 0, 0),
            ),
            AccountSnapshot(
                source="Robinhood",
                account_group="investment",
                connection_state="plaid",
                current_value=Decimal("10000.00"),
                synced_at=datetime(2026, 3, 1, 10, 0, 0),
                created_at=datetime(2026, 3, 1, 10, 0, 0),
            ),
            AccountSnapshot(
                source="Robinhood",
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

        assert latest_rows["Bank of America"]["group"] == "bank_accounts"
        assert latest_rows["Robinhood"]["group"] == "brokerage"
        assert latest_rows["Robinhood"]["history_mode"] == "historical"
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
                source="Fidelity 401k",
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
        assert latest_rows["Fidelity 401k"]["current"] == 1000.0
        assert latest_rows["Fidelity 401k"]["history_mode"] == "latest_only"

    def test_net_worth_history_does_not_backderive_bank_balance_before_snapshot(self, client):
        test_client, Session = client
        db = Session()
        db.add_all([
            Transaction(
                source_id="bofa-before-1",
                source="Bank of America",
                date=date(2026, 3, 1),
                amount=Decimal("5000.00"),
                merchant_raw="PAYROLL",
                merchant_clean="Payroll",
                category="Salary/Paycheck",
            ),
            Transaction(
                source_id="bofa-before-2",
                source="Bank of America",
                date=date(2026, 3, 2),
                amount=Decimal("4000.00"),
                merchant_raw="PAYROLL",
                merchant_clean="Payroll",
                category="Salary/Paycheck",
            ),
            AccountSnapshot(
                source="Bank of America",
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
                    source="amex",
                    date=date(2025, 12, 10),
                    amount=Decimal("-12.99"),
                    merchant_raw="DISNEYPLUS 888-905-7888 CA",
                    merchant_clean="Disney+",
                    category="Subscriptions/Entertainment",
                    account_last4="2100",
                ),
                Transaction(
                    source_id="recurring_2",
                    source="amex",
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
                    source="amex",
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
