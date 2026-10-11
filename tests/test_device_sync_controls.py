"""Device convergence controls remain distinct from bank refresh, safe and opt-in."""
import builtins
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.routes import device_sync
from src.models import get_db
from src.models.transaction import SyncLog
from src.services import job_lock, device_sync_health

@pytest.fixture
def control_client(temp_db):
    Session, _ = temp_db
    app = FastAPI()
    app.include_router(device_sync.router, prefix="/device-sync")
    def session():
        db = Session()
        try: yield db
        finally: db.close()
    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        yield client, Session

def test_paused_does_not_run_transport(control_client, monkeypatch):
    client, _ = control_client
    monkeypatch.delenv("FINANCE_APP_SYNC", raising=False)
    monkeypatch.setattr(device_sync, "sync_once", lambda db: pytest.fail("paused transport called"))
    assert client.get("/device-sync/status").json()["enabled"] is False
    assert client.post("/device-sync/run").status_code == 409

def test_status_never_refreshes_tokens_and_contains_no_secrets(control_client, monkeypatch):
    client, Session = control_client
    monkeypatch.setenv("FINANCE_APP_SYNC", "1")
    with Session() as db:
        db.add(SyncLog(source="vault_google", sync_type="google_drive", status="connected",
                       extra_data={"refresh_token":"synthetic-secret"}))
        db.commit()
        device_sync_health.record(db, successful=True)
        device_sync_health.record(db, successful=False)
    response = client.get("/device-sync/status")
    body = response.json()
    assert body["configured"] and body["enabled"] and body["last_success"]
    assert body["last_error"] and "synthetic-secret" not in response.text

def test_manual_round_cannot_overlap_a_database_job(control_client, monkeypatch):
    client, _ = control_client
    monkeypatch.setenv("FINANCE_APP_SYNC", "1")
    monkeypatch.setattr(device_sync, "sync_once", lambda db: pytest.fail("contending transport called"))
    with job_lock.try_acquire("synthetic import"):
        assert client.post("/device-sync/run").status_code == 409

def test_manual_round_reports_partial_failure_without_payload(control_client, monkeypatch):
    client, _ = control_client
    monkeypatch.setenv("FINANCE_APP_SYNC", "1")
    monkeypatch.setattr(device_sync, "sync_once", lambda db: {"ran":True,"peers_failed":["private-peer"],"error":None})
    response = client.post("/device-sync/run")
    assert response.json() == {"ran":True,"completed":False,"peer_count":0}
    assert "private-peer" not in response.text

def test_missing_optional_ai_sdk_falls_back_without_network(monkeypatch):
    from src.processing.categorizer import GeminiCategorizer
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "google.generativeai": raise ImportError("synthetic absent SDK")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    engine = GeminiCategorizer("synthetic-key")
    assert engine.categorize("Synthetic Merchant", 1) is None
    assert engine.categorize("Another Synthetic Merchant", 1) is None

@pytest.mark.parametrize("report", [
    {"awaiting_consent": True}, {"fact_conflicts": {"transactions": 1}},
    {"unresolved": {"transaction_projects": 1}}, {"rename_conflicts": 1},
])
def test_manual_round_does_not_claim_convergence_with_unresolved_peer_state(control_client, monkeypatch, report):
    client, _ = control_client
    monkeypatch.setenv("FINANCE_APP_SYNC", "1")
    monkeypatch.setattr(device_sync, "sync_once", lambda db: {"ran": True, "merges": {"synthetic-peer": report}})
    assert client.post("/device-sync/run").json()["completed"] is False


@pytest.mark.parametrize("value", ["FALSE", "No", "off", "0", "unexpected", ""])
def test_environment_cannot_accidentally_enable_cloud_writes(monkeypatch, value):
    from src.sync.runner import sync_enabled
    monkeypatch.setenv("FINANCE_APP_SYNC", value)
    assert not sync_enabled()
