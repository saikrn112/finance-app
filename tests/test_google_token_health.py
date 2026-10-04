"""The settings panel's "connected" answer, and the cache behind it.

Two opposite failures have both shipped here:

* Reporting "Connected" because a token row existed, while the refresh token was dead. No backups,
  no sync, green label, for a week.
* Reporting "Not connected" for up to a minute *after* a successful reconnect, because the cached
  failure was keyed on time alone and a new working token could not clear it. Observed live as
  three consent round trips in 35 seconds, every one of them returning 200.

So the verdict has to come from a real refresh attempt, and it has to be attached to the token it
was made about.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from src.api.routes import settings as settings_routes


class FakeLog:
    """Stands in for the `vault_google` SyncLog row."""

    def __init__(self, refresh_token: str, access_token: str = "access-1"):
        self.extra_data = {"refresh_token": refresh_token, "access_token": access_token}


@pytest.fixture(autouse=True)
def clean_cache():
    settings_routes._invalidate_token_health()
    yield
    settings_routes._invalidate_token_health()


@pytest.fixture
def refresh(monkeypatch):
    """Records refresh attempts and lets each one be made to succeed or fail."""
    calls: list[dict] = []
    outcome = {"ok": True}

    def fake(extra):
        calls.append(dict(extra))
        if not outcome["ok"]:
            raise HTTPException(status_code=401, detail="Google Drive session expired. Please reconnect.")
        return "fresh-access", dict(extra)

    monkeypatch.setattr(settings_routes, "ensure_fresh_google_access_token", fake)
    return calls, outcome


class TestHonesty:
    def test_a_row_alone_is_not_connected(self, refresh):
        calls, outcome = refresh
        outcome["ok"] = False

        ok, reason = settings_routes._google_token_health(FakeLog("dead-token"))

        assert ok is False
        assert "reconnect" in (reason or "").lower()
        assert len(calls) == 1, "the only honest answer comes from actually refreshing"

    def test_no_row_is_not_connected_without_calling_google(self, refresh):
        calls, _ = refresh
        assert settings_routes._google_token_health(None) == (False, "not connected")
        assert calls == []


class TestCaching:
    def test_repeated_polls_do_not_hammer_google(self, refresh):
        calls, _ = refresh
        log = FakeLog("live-token")

        for _ in range(5):
            assert settings_routes._google_token_health(log) == (True, None)

        assert len(calls) == 1, "the panel polls; one verdict should serve the whole window"

    def test_a_refreshed_access_token_does_not_invalidate_the_cache(self, refresh):
        """A successful refresh rewrites the access token, and callers persist it.

        Fingerprinting the whole row would therefore miss on every single check, turning the cache
        into a refresh per poll -- the opposite of why it exists.
        """
        calls, _ = refresh
        settings_routes._google_token_health(FakeLog("live-token", access_token="access-1"))
        settings_routes._google_token_health(FakeLog("live-token", access_token="access-2"))

        assert len(calls) == 1


class TestReconnectClearsAStaleFailure:
    def test_a_new_token_is_rechecked_immediately(self, refresh):
        """The reported bug: connect succeeds, panel still says not connected."""
        calls, outcome = refresh
        outcome["ok"] = False
        assert settings_routes._google_token_health(FakeLog("dead-token"))[0] is False

        # Reconnect stores a different refresh token, well inside the 60-second window.
        outcome["ok"] = True
        ok, reason = settings_routes._google_token_health(FakeLog("brand-new-token"))

        assert (ok, reason) == (True, None), "a stale failure must not outlive the token it was about"
        assert len(calls) == 2

    def test_explicit_invalidation_covers_a_reissued_identical_token(self, refresh):
        """Google may hand back the same refresh token on re-consent.

        The fingerprint is then unchanged, so only the connect handler's explicit invalidation
        rescues the panel -- which is why both mechanisms exist.
        """
        calls, outcome = refresh
        outcome["ok"] = False
        same = FakeLog("same-token")
        assert settings_routes._google_token_health(same)[0] is False

        outcome["ok"] = True
        assert settings_routes._google_token_health(same)[0] is False, "still cached, as designed"

        settings_routes._invalidate_token_health()

        assert settings_routes._google_token_health(same) == (True, None)
        assert len(calls) == 2, "the middle call was served from cache, as it should be"

    def test_the_connect_handler_invalidates(self, monkeypatch):
        """Whatever else `complete_google_drive_connect` does, it must clear the verdict."""
        import inspect

        source = inspect.getsource(settings_routes.complete_google_drive_connect)
        assert "_invalidate_token_health()" in source

    def test_a_cached_failure_does_expire_on_its_own(self, refresh, monkeypatch):
        calls, outcome = refresh
        outcome["ok"] = False
        log = FakeLog("dead-token")
        assert settings_routes._google_token_health(log)[0] is False

        outcome["ok"] = True
        # Capture the real clock before patching, or the replacement calls itself.
        later = time.monotonic() + settings_routes._TOKEN_CHECK_TTL_SECONDS + 1
        monkeypatch.setattr(settings_routes.time, "monotonic", lambda: later)

        assert settings_routes._google_token_health(log) == (True, None)
