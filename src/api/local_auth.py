"""Shared-secret gate for a backend bound to a loopback port.

The desktop shell runs this API on ``127.0.0.1:<random port>``. Any process
running as the same user can reach that port, and the API exposes complete
financial history, so a browser tab or another app that guesses the port must
not get data. The shell generates a random token per launch, passes it in via
``FINANCE_APP_LOCAL_TOKEN``, and hands the same value to the webview.

The gate installs itself only when that variable is set, so the container and
local dev flows are unchanged.
"""

from __future__ import annotations

import hmac
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN_ENV_VAR = "FINANCE_APP_LOCAL_TOKEN"
TOKEN_HEADER = "x-finance-token"
TOKEN_COOKIE = "finance_token"

# Paths reachable without the token.
#
# The OAuth callbacks are the reason this list exists at all: a provider
# redirects the *system browser* back to loopback, and that request carries
# neither our header nor our cookie. They are safe to exempt because they only
# accept a provider-issued ``code``/``state`` pair, and they are the narrowest
# possible exemption -- exact paths, not prefixes.
_EXEMPT_EXACT = frozenset(
    {
        "/",
        "/api/health",
        "/api/settings/vault/google/callback",
    }
)


def _configured_token() -> str:
    return os.environ.get(TOKEN_ENV_VAR, "")


def _presented_token(request: Request) -> str:
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header
    return request.cookies.get(TOKEN_COOKIE, "")


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    # Static frontend assets. The bundle serves the built web app from this
    # origin; withholding the shell itself would leave nothing able to present
    # the token in the first place.
    return not path.startswith("/api/")


def install_local_token_gate(app) -> bool:
    """Add the gate to ``app`` if a token is configured. Returns whether it was added."""

    expected = _configured_token()
    if not expected:
        return False

    @app.middleware("http")
    async def _local_token_gate(request: Request, call_next):
        if request.method == "OPTIONS" or _is_exempt(request.url.path):
            return await call_next(request)
        # compare_digest to keep the comparison constant-time.
        if not hmac.compare_digest(_presented_token(request), expected):
            # No detail in the body and no request identifiers in the log: the
            # caller already knows what it sent, and the repo's privacy rules
            # forbid logging query strings that can carry identifiers.
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    return True
