"""Tests for the privacy-mask middleware's response handling.

The mask itself is covered elsewhere; this file is about the HTTP framing, because that is
where it was broken. The middleware copied the *original* `Content-Length` onto a masked body
of a different length, so h11 aborted with "Too much data for declared Content-Length" and the
response died. Every masked response was affected — fake amounts are essentially never the same
width as real ones — which meant `FINANCE_APP_PRIVACY_MASK` did not work at all.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def masked_app(monkeypatch):
    """A fresh app with the mask enabled.

    The middleware is installed at import time behind a module-level flag, so the modules have
    to be reloaded after setting the variable rather than toggled afterwards.
    """
    monkeypatch.setenv("FINANCE_APP_PRIVACY_MASK", "1")

    import src.privacy_mask
    import src.api.server

    importlib.reload(src.privacy_mask)
    server = importlib.reload(src.api.server)
    assert src.privacy_mask.PRIVACY_MASK_ENABLED, "the mask should be on for these tests"

    yield server.app

    # Put both modules back, or every later test in the session runs with the mask on.
    monkeypatch.delenv("FINANCE_APP_PRIVACY_MASK", raising=False)
    importlib.reload(src.privacy_mask)
    importlib.reload(src.api.server)


class TestMaskedResponseFraming:
    def test_a_masked_json_response_is_delivered_intact(self, masked_app):
        """The regression. A wrong Content-Length aborts the response mid-flight."""
        with TestClient(masked_app) as client:
            response = client.get("/api/meta")

        assert response.status_code == 200
        # Readable at all, and self-consistent: a truncated body fails to parse.
        payload = response.json()
        assert isinstance(payload, dict)

    def test_content_length_matches_the_masked_body(self, masked_app):
        with TestClient(masked_app) as client:
            response = client.get("/api/meta")

        declared = response.headers.get("content-length")
        assert declared is not None, "a fixed-length response should declare its length"
        assert int(declared) == len(response.content)

    def test_the_body_really_is_json(self, masked_app):
        """Guards against the mask emitting something that only looks like a payload."""
        with TestClient(masked_app) as client:
            response = client.get("/api/meta")
        json.loads(response.content)

    def test_health_still_answers_under_the_mask(self, masked_app):
        """The desktop shell polls this to decide the backend is up.

        When the framing bug was live the app never reached "ready", because the very first
        masked response killed the connection.
        """
        with TestClient(masked_app) as client:
            response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_non_json_responses_pass_through_untouched(self, masked_app):
        """Only JSON is masked; a static or file response must not be rewritten."""
        with TestClient(masked_app) as client:
            response = client.get("/api/plugin-icons/definitely-not-a-real-icon.png")
        # 404 either way; the point is that it is not mangled into a JSON body with a
        # mismatched length.
        assert response.status_code == 404
