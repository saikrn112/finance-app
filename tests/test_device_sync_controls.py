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


# --- the "sync has stopped" banner -----------------------------------------------------------------
#
# Device sync was dead for a week (both Google sign-ins had expired) and nothing on screen said so:
# each app kept pulling from Plaid, so everything looked current while no edit crossed between them.

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)


def _verdict(state, **kw):
    args = dict(enabled=True, configured=True, peer_count=1, now=NOW)
    args.update(kw)
    return device_sync_health.attention(state, **args)


class TestAttention:
    def test_a_recent_success_is_quiet(self):
        assert _verdict({"last_success": (NOW - timedelta(minutes=6)).isoformat()})["needed"] is False

    def test_a_week_without_success_is_raised(self):
        """The case that went unnoticed: last success a week ago, no error stored."""
        v = _verdict({"last_success": (NOW - timedelta(days=7)).isoformat()})
        assert v["needed"] is True and "recently" in v["reason"]

    def test_one_slow_round_does_not_cry_wolf(self):
        assert _verdict({"last_success": (NOW - timedelta(minutes=20)).isoformat()})["needed"] is False

    def test_an_expired_google_sign_in_says_so_specifically(self):
        v = _verdict({"last_success": (NOW - timedelta(minutes=2)).isoformat()}, google_expired=True)
        assert v["needed"] is True and "Reconnect" in v["reason"]

    def test_a_failure_after_the_last_success_is_raised_with_its_reason(self):
        v = _verdict({
            "last_success": (NOW - timedelta(minutes=30)).isoformat(),
            "last_attempt": (NOW - timedelta(minutes=2)).isoformat(),
            "last_error": "Review the first device merge in Settings before these devices can converge.",
        })
        assert v["needed"] is True and "first device merge" in v["reason"]

    def test_paused_on_purpose_is_not_a_fault(self):
        assert _verdict({}, enabled=False)["needed"] is False

    def test_a_lone_device_that_never_synced_is_quiet(self):
        assert _verdict({}, peer_count=0)["needed"] is False

    def test_a_device_with_peers_that_never_synced_is_raised(self):
        assert _verdict({}, peer_count=2)["needed"] is True


def test_status_reports_attention_without_refreshing_tokens(control_client, monkeypatch):
    client, Session = control_client
    monkeypatch.setenv("FINANCE_APP_SYNC", "1")
    monkeypatch.setattr("src.api.routes.settings.ensure_fresh_google_access_token",
                        lambda *a, **k: pytest.fail("status must not refresh tokens"))
    with Session() as db:
        db.add(SyncLog(source="vault_google", sync_type="google_drive", status="connected",
                       extra_data={"refresh_token": "synthetic-secret"}))
        db.commit()
        device_sync_health.record(db, successful=False)
    body = client.get("/device-sync/status").json()
    assert body["attention"]["needed"] is True
    assert "synthetic-secret" not in str(body)


def test_a_known_dead_token_is_named_in_the_banner(control_client, monkeypatch):
    from src.api.routes import settings as settings_routes

    client, Session = control_client
    monkeypatch.setenv("FINANCE_APP_SYNC", "1")
    with Session() as db:
        log = SyncLog(source="vault_google", sync_type="google_drive", status="connected",
                      extra_data={"refresh_token": "synthetic-secret"})
        db.add(log)
        db.commit()
        fingerprint = settings_routes._token_fingerprint(log)
        device_sync_health.record(db, successful=True)
    monkeypatch.setattr(settings_routes, "_token_health", (1.0, fingerprint, False, "expired"))

    assert "Reconnect" in client.get("/device-sync/status").json()["attention"]["reason"]


class TestStuckJobs:
    """A Plaid pull hung on a dead connection held the job lock for 22 hours; the banner blamed Google."""

    def test_a_job_holding_the_lock_too_long_is_named(self):
        v = _verdict({"last_success": (NOW - timedelta(hours=22)).isoformat()},
                     busy_job="plaid sync", busy_seconds=22 * 3600)
        assert v["needed"] is True and "plaid sync" in v["reason"] and "stuck" in v["reason"]

    def test_a_normal_length_job_is_not_called_stuck(self):
        v = _verdict({"last_success": (NOW - timedelta(minutes=3)).isoformat()},
                     busy_job="plaid sync", busy_seconds=90)
        assert v["needed"] is False


def test_the_lock_reports_how_long_it_has_been_held():
    from src.services import job_lock

    assert job_lock.held_for_seconds() is None
    with job_lock.try_acquire("test job") as acquired:
        assert acquired and job_lock.held_for_seconds() >= 0
    assert job_lock.held_for_seconds() is None


def test_plaid_calls_get_a_timeout_by_default(monkeypatch):
    """plaid-python sends timeout=None (wait forever) unless told otherwise."""
    from src.ingestion import plaid_client

    seen = {}

    class FakeRest:
        def request(self, *args, **kwargs):
            seen.update(kwargs)

    class FakeClient:
        rest_client = FakeRest()

    client = FakeClient()
    plaid_client._apply_default_timeout(client)
    client.rest_client.request("POST", "https://example.invalid")
    assert seen["_request_timeout"] == plaid_client.PLAID_TIMEOUT

    client.rest_client.request("POST", "https://example.invalid", _request_timeout=5)
    assert seen["_request_timeout"] == 5, "an explicit timeout is respected"


def test_every_drive_call_has_a_timeout():
    import inspect
    import re

    from src.vault import google_drive

    calls = re.findall(r"urlopen\(([^)]*)\)", inspect.getsource(google_drive))
    assert calls and all("timeout=" in c for c in calls), calls


def test_the_process_has_a_socket_timeout_backstop():
    import socket

    import src.api.server  # noqa: F401  (sets it on import)

    assert socket.getdefaulttimeout() is not None
