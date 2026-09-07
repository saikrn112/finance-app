"""Tests for the loopback shared-secret gate used by the macOS desktop shell."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import local_auth
from src.api.local_auth import (
    TOKEN_COOKIE,
    TOKEN_ENV_VAR,
    TOKEN_HEADER,
    install_local_token_gate,
)

TOKEN = "test-token-not-a-real-secret"


def _app_with_gate(monkeypatch, token=TOKEN):
    """A minimal app carrying the gate, so these tests don't depend on the real router tree."""
    if token is None:
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(TOKEN_ENV_VAR, token)

    app = FastAPI()
    installed = install_local_token_gate(app)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta():
        return {"mode": "live"}

    @app.get("/")
    def root():
        return {"status": "ok"}

    @app.get("/assets/app.js")
    def asset():
        return {"static": True}

    @app.get("/api/settings/vault/google/callback")
    def google_callback():
        return {"callback": True}

    return app, installed


class TestGateInstallation:
    def test_not_installed_without_token(self, monkeypatch):
        """The container and local dev flows must be completely unaffected."""
        app, installed = _app_with_gate(monkeypatch, token=None)
        assert installed is False
        with TestClient(app) as c:
            assert c.get("/api/meta").status_code == 200

    def test_not_installed_for_empty_token(self, monkeypatch):
        """An empty value is 'not configured', not 'the token is the empty string'."""
        app, installed = _app_with_gate(monkeypatch, token="")
        assert installed is False
        with TestClient(app) as c:
            assert c.get("/api/meta").status_code == 200

    def test_installed_with_token(self, monkeypatch):
        _, installed = _app_with_gate(monkeypatch)
        assert installed is True


class TestGateEnforcement:
    def test_api_request_without_token_is_rejected(self, monkeypatch):
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            response = c.get("/api/meta")
        assert response.status_code == 401

    def test_wrong_token_is_rejected(self, monkeypatch):
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            response = c.get("/api/meta", headers={TOKEN_HEADER: "wrong"})
        assert response.status_code == 401

    def test_token_prefix_is_rejected(self, monkeypatch):
        """Guards against a comparison that only checks a prefix."""
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            response = c.get("/api/meta", headers={TOKEN_HEADER: TOKEN[:-1]})
        assert response.status_code == 401

    def test_header_token_is_accepted(self, monkeypatch):
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            response = c.get("/api/meta", headers={TOKEN_HEADER: TOKEN})
        assert response.status_code == 200

    def test_cookie_token_is_accepted(self, monkeypatch):
        """The webview presents the token as a cookie, so page loads carry it too."""
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            c.cookies.set(TOKEN_COOKIE, TOKEN)
            response = c.get("/api/meta")
        assert response.status_code == 200

    def test_rejection_body_leaks_nothing(self, monkeypatch):
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            response = c.get("/api/meta")
        assert response.json() == {"detail": "unauthorized"}
        assert TOKEN not in response.text


class TestExemptPaths:
    """The exemptions exist for provider redirects that cannot carry our token."""

    @pytest.mark.parametrize(
        "path",
        ["/api/health", "/", "/api/settings/vault/google/callback", "/assets/app.js"],
    )
    def test_exempt_paths_reachable_without_token(self, monkeypatch, path):
        app, _ = _app_with_gate(monkeypatch)
        with TestClient(app) as c:
            assert c.get(path).status_code == 200

    def test_exemptions_are_exact_not_prefixes(self):
        """`/api/health` being exempt must not exempt `/api/healthy-transactions`."""
        assert local_auth._is_exempt("/api/health") is True
        assert local_auth._is_exempt("/api/healthz") is False
        assert local_auth._is_exempt("/api/settings/vault/google/callback") is True
        assert local_auth._is_exempt("/api/settings/vault/google/callback/steal") is False
        assert local_auth._is_exempt("/api/transactions") is False

    def test_non_api_paths_are_exempt(self):
        """The bundle serves the built frontend from this origin; it has to load."""
        assert local_auth._is_exempt("/index.html") is True
        assert local_auth._is_exempt("/assets/index-abc123.js") is True
