"""Splitwise OAuth2 + REST client.

Deliberately mirrors src/vault/google_drive.py: same pending-state-on-disk CSRF handling,
same `ensure_fresh_*_token` shape, same "raise HTTPException with the provider's message"
error style. Optional integration — with no credentials configured, every entry point fails
closed and the rest of the app is unaffected.

Endpoint paths and payload shapes are centralised in the constants and
`build_expense_payload` below so they can be corrected in one place if the live API differs.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException

from src.config import settings

@dataclass(frozen=True)
class Credentials:
    """OAuth app credentials. Sourced from the DB when entered in the UI, else config.yaml."""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    api_key: str = ""

    @property
    def has_oauth(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def usable(self) -> bool:
        return self.has_oauth or self.has_api_key


def credentials_from_settings() -> Credentials:
    return Credentials(
        client_id=settings.splitwise.client_id,
        client_secret=settings.splitwise.client_secret,
        redirect_uri=settings.splitwise.redirect_uri,
    )


SPLITWISE_AUTH_URL = "https://secure.splitwise.com/oauth/authorize"
SPLITWISE_TOKEN_URL = "https://secure.splitwise.com/oauth/token"
SPLITWISE_API_BASE = "https://secure.splitwise.com/api/v3.0"

_STATE_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _require_oauth(creds: Credentials) -> None:
    if not creds.has_oauth:
        raise HTTPException(
            status_code=400,
            detail="Splitwise is not configured. Add your app's Client ID and Secret in Settings.",
        )


def create_auth_url(creds: Credentials) -> str:
    """Authorization-code URL with a one-shot state token for CSRF."""
    _require_oauth(creds)
    state = secrets.token_urlsafe(24)
    _write_pending_state(state, {"created_at": datetime.now(timezone.utc).isoformat()})
    query = {
        "client_id": creds.client_id,
        "redirect_uri": creds.redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{SPLITWISE_AUTH_URL}?{urlencode(query)}"


def validate_state(state: str) -> dict[str, Any]:
    payload = _read_pending_state(state)
    if payload is None:
        raise HTTPException(status_code=400, detail="Splitwise sign-in expired or was already used")
    created = payload.get("created_at")
    if created:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(created)
        if age.total_seconds() > _STATE_TTL_SECONDS:
            delete_pending_state(state)
            raise HTTPException(status_code=400, detail="Splitwise sign-in expired; try again")
    return payload


def exchange_code(code: str, creds: Credentials) -> dict[str, Any]:
    _require_oauth(creds)
    payload = urlencode({
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "code": code,
        "redirect_uri": creds.redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    response = _json_request(Request(
        SPLITWISE_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    ))
    if not response.get("access_token"):
        raise HTTPException(status_code=502, detail="Splitwise did not return an access token")
    return response


def refresh_token(refresh: str, creds: Credentials) -> dict[str, Any]:
    _require_oauth(creds)
    payload = urlencode({
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode()
    try:
        response = _json_request(Request(
            SPLITWISE_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        ))
    except HTTPException as exc:
        # Same reasoning as the Google path: a dead refresh token is a re-auth prompt, not a
        # 500. Surfacing 401 lets the UI offer "reconnect" instead of showing a stack trace.
        raise HTTPException(status_code=401, detail="Splitwise session expired. Please reconnect.") from exc
    if not response.get("access_token"):
        raise HTTPException(status_code=401, detail="Splitwise session expired. Please reconnect.")
    return response


def token_payload_to_extra(response: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "provider": "splitwise",
        "access_token": response.get("access_token"),
        "refresh_token": response.get("refresh_token"),
        "token_type": response.get("token_type"),
    }
    expires_in = response.get("expires_in")
    if expires_in:
        extra["expires_at"] = _expiry_timestamp(int(expires_in))
    if user:
        extra["splitwise_user_id"] = user.get("id")
        extra["email"] = user.get("email")
        extra["name"] = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None
    return extra


def ensure_fresh_access_token(
    extra_data: dict[str, Any],
    creds: Credentials | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a usable access token, refreshing in place when it is close to expiring.

    A personal API key short-circuits everything: it never expires and needs no refresh,
    which is the simpler path for a single-user install.
    """
    creds = creds or credentials_from_settings()
    if creds.has_api_key:
        return creds.api_key, extra_data
    access_token = extra_data.get("access_token")
    expires_at = extra_data.get("expires_at")
    refresh = extra_data.get("refresh_token")

    if access_token and not expires_at:
        # Splitwise tokens have historically been long-lived and issued without an expiry.
        # With nothing to check, use it and let a 401 from the API trigger re-auth.
        return access_token, extra_data
    if access_token and expires_at:
        try:
            from datetime import timedelta
            if datetime.now(timezone.utc) < datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            ) - timedelta(minutes=2):
                return access_token, extra_data
        except (ValueError, AttributeError):
            pass
    if not refresh:
        raise HTTPException(status_code=401, detail="Splitwise session expired. Please reconnect.")
    refreshed = refresh_token(refresh, creds)
    updated = dict(extra_data)
    updated["access_token"] = refreshed["access_token"]
    if refreshed.get("refresh_token"):
        updated["refresh_token"] = refreshed["refresh_token"]
    if refreshed.get("expires_in"):
        updated["expires_at"] = _expiry_timestamp(int(refreshed["expires_in"]))
    return updated["access_token"], updated


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def get_current_user(access_token: str) -> dict[str, Any]:
    return _api(access_token, "get_current_user").get("user") or {}


def get_friends(access_token: str) -> list[dict[str, Any]]:
    return _api(access_token, "get_friends").get("friends") or []


def get_groups(access_token: str) -> list[dict[str, Any]]:
    return _api(access_token, "get_groups").get("groups") or []


def create_group(access_token: str, *, name: str, member_user_ids: list[int]) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "group_type": "trip"}
    for index, user_id in enumerate(member_user_ids):
        payload[f"users__{index}__user_id"] = user_id
    group = _api(access_token, "create_group", payload).get("group") or {}
    if not group.get("id"):
        raise HTTPException(status_code=502, detail="Splitwise did not return a group id")
    return group


def build_expense_payload(
    *,
    cost: Decimal,
    description: str,
    group_id: int | None,
    payer_user_id: int,
    owed_shares: dict[int, Decimal],
    expense_date: date | None = None,
    currency_code: str = "USD",
) -> dict[str, Any]:
    """Assemble a create/update_expense body.

    Splitwise requires both sides to reconcile exactly: the paid shares must sum to `cost`
    and so must the owed shares. One payer here (the card holder), so their paid share is
    the whole cost while every participant carries their own owed share. Any rounding
    remainder is pushed onto the payer, who is the person who actually fronted the money.
    """
    cost = _money(cost)
    payload: dict[str, Any] = {
        "cost": f"{cost:.2f}",
        "description": description or "Expense",
        "currency_code": currency_code,
    }
    if group_id:
        payload["group_id"] = group_id
    if expense_date:
        payload["date"] = expense_date.isoformat()

    shares = {user_id: _money(amount) for user_id, amount in owed_shares.items()}
    if payer_user_id not in shares:
        shares[payer_user_id] = Decimal("0.00")

    drift = cost - sum(shares.values())
    if drift:
        shares[payer_user_id] = _money(shares[payer_user_id] + drift)

    for index, (user_id, owed) in enumerate(shares.items()):
        payload[f"users__{index}__user_id"] = user_id
        payload[f"users__{index}__paid_share"] = f"{cost:.2f}" if user_id == payer_user_id else "0.00"
        payload[f"users__{index}__owed_share"] = f"{owed:.2f}"
    return payload


def create_expense(access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = _api(access_token, "create_expense", payload)
    _raise_for_expense_errors(data)
    expenses = data.get("expenses") or []
    if not expenses:
        raise HTTPException(status_code=502, detail="Splitwise created no expense")
    return expenses[0]


def update_expense(access_token: str, expense_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    data = _api(access_token, f"update_expense/{expense_id}", payload)
    _raise_for_expense_errors(data)
    expenses = data.get("expenses") or []
    return expenses[0] if expenses else {}


def delete_expense(access_token: str, expense_id: int) -> None:
    _api(access_token, f"delete_expense/{expense_id}", {})


def _raise_for_expense_errors(data: dict[str, Any]) -> None:
    errors = data.get("errors") or {}
    if errors:
        # Splitwise returns {"errors": {"base": ["..."]}} rather than an HTTP error status.
        flat = "; ".join(
            f"{key}: {', '.join(value) if isinstance(value, list) else value}"
            for key, value in errors.items()
        )
        raise HTTPException(status_code=502, detail=f"Splitwise rejected the expense — {flat}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _api(access_token: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{SPLITWISE_API_BASE}/{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    if payload is None:
        request = Request(url, headers=headers, method="GET")
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=urlencode(payload).encode(), headers=headers, method="POST")
    return _json_request(request)


def _json_request(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return json.loads(body) if body else {}
    except HTTPException:
        raise
    except HTTPError as exc:
        detail = f"Splitwise request failed ({exc.code})"
        try:
            body = exc.read().decode("utf-8", "ignore").strip()
            if body:
                detail = f"{detail}: {body[:500]}"
        except Exception:
            pass
        status = 401 if exc.code in (401, 403) else 502
        raise HTTPException(status_code=status, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Splitwise request failed: {exc}") from exc


def _expiry_timestamp(expires_in_seconds: int) -> str:
    from datetime import timedelta
    return (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pending_state_dir() -> Path:
    path = Path(settings.app.runtime_dir) / ".oauth" / "splitwise"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_state_path(state: str) -> Path:
    safe = "".join(ch for ch in state if ch.isalnum() or ch in "-_")
    return _pending_state_dir() / f"{safe}.json"


def _write_pending_state(state: str, payload: dict[str, Any]) -> None:
    _pending_state_path(state).write_text(json.dumps(payload))


def _read_pending_state(state: str) -> dict[str, Any] | None:
    path = _pending_state_path(state)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def delete_pending_state(state: str) -> None:
    _pending_state_path(state).unlink(missing_ok=True)
