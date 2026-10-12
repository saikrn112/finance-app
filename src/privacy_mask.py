"""Privacy masking layer for financial amounts.

When FINANCE_APP_PRIVACY_MASK=true is set, all numeric financial values
returned from API responses are deterministically masked. The running app
should NEVER have this set — it's only for ad-hoc debug/CLI access.

Usage:
    # Ad-hoc debug with masked values:
    FINANCE_APP_PRIVACY_MASK=true finch exec finances-dev2-app-1 python3 -c "..."
"""

import hashlib
import json
import os
from decimal import Decimal

PRIVACY_MASK_ENABLED = os.environ.get("FINANCE_APP_PRIVACY_MASK", "").lower() in (
    "true",
    "1",
    "yes",
)

AMOUNT_FIELDS = {
    "amount",
    "balance",
    "current_value",
    "ledger_balance",
    "snapshot_balance",
    "beginning_balance",
    "ending_balance",
    "gross",
    "net",
    "total_taxes",
    "total_deductions",
    "value",
    "cost_basis",
    "price",
    "rate",
    "monthly_amount",
    "annual_amount",
    "total_amount",
    "remaining_amount",
    "paid_amount",
    "budgeted_amount",
    "actual_amount",
    "difference",
    "total",
    "subtotal",
    "tax",
    "tip",
    "fee",
    "interest",
    "principal",
    "payment",
    "contribution",
    "withdrawal",
    "deposit",
    "transfer_amount",
}


def mask_amount(value: float, seed: str = "") -> float:
    """Deterministically mask a financial amount.

    Preserves: sign, rough magnitude (same number of digits), 2 decimal places.
    Same input+seed always returns same output.
    """
    if not PRIVACY_MASK_ENABLED or value is None:
        return value
    if value == 0:
        return 0.0

    sign = 1 if value >= 0 else -1
    abs_val = abs(value)

    # Determine magnitude (number of digits before decimal)
    magnitude = len(str(int(abs_val))) if abs_val >= 1 else 0

    # Hash the value + seed to get deterministic pseudo-random bytes
    hash_input = f"{value:.2f}:{seed}".encode()
    hash_bytes = hashlib.sha256(hash_input).digest()

    # Use hash bytes to generate a number in the same magnitude range
    hash_int = int.from_bytes(hash_bytes[:8], "big")

    if magnitude == 0:
        # Sub-dollar amount (0.xx)
        masked = (hash_int % 99 + 1) / 100.0
    else:
        # Same number of digits
        low = 10 ** (magnitude - 1)
        high = 10**magnitude - 1
        masked = low + (hash_int % (high - low + 1))
        # Add cents
        cents = (hash_int >> 32) % 100
        masked = masked + cents / 100.0

    return round(sign * masked, 2)


#: Numbers that describe structure, not money, and must survive masking for the UI to work.
#:
#: Masking used to be opt-in: only keys in AMOUNT_FIELDS were replaced. Any money field named
#: anything else went through as-is, and the dashboard's own cards did exactly that --
#: `income`, `spending`, `net_flow`, `core_spending`, and the I/O chart's series -- so "private
#: mode" showed real totals on its most prominent screen. Now every number is masked unless its
#: key says it is structural. A new money field is private by default; a new structural field at
#: worst shows a scrambled count, which is the right way round for a privacy feature.
STRUCTURAL_KEYS = frozenset({
    "id", "count", "n", "page", "pages", "limit", "offset", "page_size", "total_count",
    "total_pages", "year", "month", "day", "week", "quarter", "index", "rank", "order",
    "position", "version", "format_version", "retry", "port", "time", "timestamp",
    "peer_count", "rows", "length", "width", "height", "decimals", "precision", "days",
    "hours", "minutes", "seconds", "age_days", "status_code", "code",
})
STRUCTURAL_SUFFIXES = (
    "_id", "_ids", "_count", "_index", "_year", "_month", "_day", "_days", "_at", "_ts",
    "_version", "_pages", "_rank", "_order", "_seconds", "_minutes", "_hours", "_ms",
)
STRUCTURAL_PREFIXES = ("num_", "n_", "is_", "has_")


def _is_structural(key: str | None) -> bool:
    if not key:
        return False
    k = key.lower()
    return k in STRUCTURAL_KEYS or k.endswith(STRUCTURAL_SUFFIXES) or k.startswith(STRUCTURAL_PREFIXES)


def _mask_value(key: str | None, value, seed: str):
    # bool is an int subclass; True/False are flags, never amounts.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return value
    if _is_structural(key):
        return value
    return mask_amount(float(value), seed=f"{key}:{seed}")


def mask_row(row_dict: dict, seed: str = "") -> dict:
    """Mask every numeric value in a dict that is not structurally named. Recurses."""
    if not PRIVACY_MASK_ENABLED:
        return row_dict
    if not isinstance(row_dict, dict):
        return row_dict

    masked = {}
    for key, value in row_dict.items():
        if isinstance(value, dict):
            masked[key] = mask_row(value, seed=seed)
        elif isinstance(value, list):
            masked[key] = _mask_list(value, seed=f"{key}:{seed}", key=key)
        else:
            masked[key] = _mask_value(key, value, seed)
    return masked


def _mask_list(items: list, seed: str = "", key: str | None = None) -> list:
    """Mask a list. Bare numbers take the list's own key -- a chart series of raw values leaked
    entirely before, because only dict entries were ever inspected."""
    result = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            result.append(mask_row(item, seed=f"{seed}[{i}]"))
        elif isinstance(item, list):
            result.append(_mask_list(item, seed=f"{seed}[{i}]", key=key))
        else:
            result.append(_mask_value(key, item, f"{seed}[{i}]"))
    return result


def mask_json_body(body: bytes, seed: str = "") -> bytes:
    """Mask financial amounts in a JSON response body.

    Returns the masked body as bytes, or the original body if parsing fails.
    """
    if not PRIVACY_MASK_ENABLED:
        return body
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            data = mask_row(data, seed=seed)
        elif isinstance(data, list):
            data = _mask_list(data, seed=seed)
        return json.dumps(data).encode()
    except (json.JSONDecodeError, TypeError):
        return body
