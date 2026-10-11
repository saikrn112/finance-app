from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import reconciliation
from src.models import Balance, ExchangeRate, get_db


@pytest.fixture
def reconciliation_client(temp_db, monkeypatch):
    factory, _ = temp_db
    app = FastAPI()
    app.include_router(reconciliation.router, prefix='/reconciliation')
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(reconciliation, 'ensure_rates_fresh', lambda db: None)
    with TestClient(app) as client:
        yield client, factory


def test_balance_requires_currency_and_preserves_exact_account_identity(reconciliation_client):
    client, factory = reconciliation_client
    parameters = dict(source='Synthetic Bank', balance_date='2026-01-31', balance='100.01', account_last4='1001')
    assert client.post('/reconciliation/balance', params=parameters).status_code == 422
    assert client.post('/reconciliation/balance', params={**parameters, 'currency': 'EUR'}).status_code == 200
    with factory() as db:
        row = db.query(Balance).one()
        assert row.currency == 'EUR' and row.account_last4 == '1001' and row.balance == Decimal('100.01')


def test_balances_convert_known_currency_and_do_not_guess_legacy_currency(reconciliation_client):
    client, factory = reconciliation_client
    with factory() as db:
        db.add_all([Balance(source='Synthetic Bank', date=date(2026, 1, 31), balance=100, currency='EUR'),
                    Balance(source='Synthetic Bank', date=date(2026, 1, 31), balance=100),
                    ExchangeRate(date=date(2026, 1, 31), from_currency='EUR', to_currency='USD', rate=2)])
        db.commit()
    assert client.get('/reconciliation/balances').status_code == 422
    rows = client.get('/reconciliation/balances', params={'currency': 'USD'}).json()
    assert sorted(str(row['balance']) for row in rows) == ['200.0', 'None']
    assert next(row for row in rows if row['balance'] is None)['status'] == 'currency_unknown'


def test_statement_verification_is_read_only_and_requires_currency(reconciliation_client):
    client, factory = reconciliation_client
    body = dict(source='Synthetic Bank', account_last4='1001', period_start='2026-01-01', period_end='2026-01-31',
                opening_balance='100', closing_balance='100', transactions=[])
    assert client.post('/reconciliation/verify-statement', json=body).status_code == 422
    result = client.post('/reconciliation/verify-statement?currency=USD', json=body)
    assert result.status_code == 200 and result.json()['status'] == 'verified'
    with factory() as db:
        assert db.query(Balance).count() == 0
