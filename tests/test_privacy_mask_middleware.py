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


# --- fail closed --------------------------------------------------------------------------------
#
# Private mode showed the dashboard's real Inflow/Outflow/Core/Net Flow and the I/O chart, because
# only keys on a fixed allowlist were masked. Every number is now masked unless structural.

import json as _json

import src.privacy_mask as _pm


@pytest.fixture
def masking(monkeypatch):
    monkeypatch.setattr(_pm, "PRIVACY_MASK_ENABLED", True)


def _mask(payload):
    return _json.loads(_pm.mask_json_body(_json.dumps(payload).encode(), seed="/api/x"))


def test_the_dashboard_summary_fields_are_masked(masking):
    real = {"income": 11841.94, "spending": 9082.09, "net_flow": 2759.85,
            "core_spending": 4487.09, "subscriptions_monthly": 80.08, "total_balance": 175000.0}
    masked = _mask(real)
    for key, value in real.items():
        assert masked[key] != value, f"{key} leaked its real value"


def test_a_field_nobody_has_named_yet_is_private_by_default(masking):
    assert _mask({"brand_new_money_field": 1234.56})["brand_new_money_field"] != 1234.56


def test_bare_numbers_in_a_chart_series_are_masked(masking):
    out = _mask({"series": {"inflow": [1600.0, 0.0, 4000.0], "outflow": [100.0, 250.5]}})
    assert out["series"]["inflow"][0] != 1600.0 and out["series"]["inflow"][2] != 4000.0
    assert out["series"]["outflow"] != [100.0, 250.5]


def test_structure_survives_so_the_ui_still_works(masking):
    payload = {"id": 42, "transaction_id": 7, "count": 3, "page": 2, "year": 2026,
               "pending": True, "is_recurring": False, "peer_count": 2, "time": 1760000000,
               "format_version": 1, "rows": [{"id": 1, "amount": -12.34}]}
    out = _mask(payload)
    for key in ("id", "transaction_id", "count", "page", "year", "pending", "is_recurring",
                "peer_count", "time", "format_version"):
        assert out[key] == payload[key], f"{key} is structural and must not change"
    assert out["rows"][0]["id"] == 1 and out["rows"][0]["amount"] != -12.34


def test_masking_keeps_sign_and_is_deterministic(masking):
    a = _mask({"net_flow": -2759.85})["net_flow"]
    assert a < 0 and a == _mask({"net_flow": -2759.85})["net_flow"]
