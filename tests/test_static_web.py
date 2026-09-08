"""Tests for serving the built frontend from the API's own origin (macOS bundle)."""
import pytest
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from src.api.static_web import (
    WEB_DIR_ENV_VAR,
    configured_web_directory,
    index_file,
    mount_static_frontend,
)


@pytest.fixture
def built_frontend(tmp_path):
    """A minimal stand-in for `vite build` output."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Finance</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('app')")
    return tmp_path


class TestConfiguration:
    def test_disabled_without_env_var(self, monkeypatch):
        """The container and local dev flows must keep using Vite."""
        monkeypatch.delenv(WEB_DIR_ENV_VAR, raising=False)
        assert configured_web_directory() is None
        assert index_file() is None

    def test_enabled_when_pointed_at_a_build(self, monkeypatch, built_frontend):
        monkeypatch.setenv(WEB_DIR_ENV_VAR, str(built_frontend))
        assert configured_web_directory() == built_frontend
        assert index_file() == built_frontend / "index.html"

    def test_directory_without_index_is_rejected(self, monkeypatch, tmp_path):
        """A half-copied build must read as 'no build', not as a working app.

        Mounting it would serve 404s for the app shell itself, which looks like a
        broken application rather than a bundle built without `vite build`.
        """
        monkeypatch.setenv(WEB_DIR_ENV_VAR, str(tmp_path))
        assert configured_web_directory() is None

    def test_nonexistent_directory_is_rejected(self, monkeypatch):
        monkeypatch.setenv(WEB_DIR_ENV_VAR, "/nonexistent/web")
        assert configured_web_directory() is None

    def test_empty_env_var_is_rejected(self, monkeypatch):
        monkeypatch.setenv(WEB_DIR_ENV_VAR, "")
        assert configured_web_directory() is None


class TestMounting:
    def _app(self, monkeypatch, directory):
        """An app shaped like src/api/server.py: API routes, then the "/" route, then the mount."""
        monkeypatch.setenv(WEB_DIR_ENV_VAR, str(directory))
        app = FastAPI()

        @app.get("/api/meta")
        def meta():
            return {"mode": "live"}

        @app.get("/")
        def root(code: str | None = Query(default=None), state: str | None = Query(default=None)):
            if code and state:
                return {"oauth": True}
            if (index := index_file()) is not None:
                return FileResponse(index)
            return {"status": "ok"}

        mount_static_frontend(app)
        return app

    def test_root_serves_the_app_shell(self, monkeypatch, built_frontend):
        with TestClient(self._app(monkeypatch, built_frontend)) as client:
            response = client.get("/")
        assert response.status_code == 200
        assert "<title>Finance</title>" in response.text

    def test_assets_are_served(self, monkeypatch, built_frontend):
        with TestClient(self._app(monkeypatch, built_frontend)) as client:
            response = client.get("/assets/index-abc123.js")
        assert response.status_code == 200
        assert "console.log" in response.text

    def test_the_mount_does_not_shadow_the_api(self, monkeypatch, built_frontend):
        """The reason the mount is registered last.

        Starlette matches routes in registration order, so a mount at "/" added before
        the routers would swallow every API request.
        """
        with TestClient(self._app(monkeypatch, built_frontend)) as client:
            response = client.get("/api/meta")
        assert response.status_code == 200
        assert response.json() == {"mode": "live"}

    def test_the_oauth_callback_still_wins_at_the_root(self, monkeypatch, built_frontend):
        """The callback is identified by its query parameters, not by a distinct path."""
        with TestClient(self._app(monkeypatch, built_frontend)) as client:
            response = client.get("/", params={"code": "abc", "state": "xyz"})
        assert response.status_code == 200
        assert response.json() == {"oauth": True}

    def test_no_mount_without_a_build(self, monkeypatch):
        monkeypatch.delenv(WEB_DIR_ENV_VAR, raising=False)
        app = FastAPI()

        @app.get("/")
        def root():
            if (index := index_file()) is not None:
                return FileResponse(index)
            return {"status": "ok", "service": "finance-app-api"}

        assert mount_static_frontend(app) is None
        with TestClient(app) as client:
            assert client.get("/").json() == {"status": "ok", "service": "finance-app-api"}

    def test_traversal_outside_the_build_is_refused(self, monkeypatch, built_frontend, tmp_path):
        """The mount must not become a file server for the whole disk."""
        secret = tmp_path.parent / "outside-the-build.txt"
        secret.write_text("not for the browser")
        with TestClient(self._app(monkeypatch, built_frontend)) as client:
            response = client.get(f"/../{secret.name}")
        assert response.status_code in (404, 403, 400)
        assert "not for the browser" not in response.text
