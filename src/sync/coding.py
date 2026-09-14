"""Encoding values for the wire.

Two rules that matter more than they look:

* **Money is a string, never a float.** `Decimal("-10.01")` through a float and back is not
  reliably `-10.01`, and this payload carries real balances. Amounts travel as their exact decimal
  string, in the transaction's own currency, with no conversion -- converting on the wire would bake
  one device's rate table into another device's history.
* **Timestamps are naive UTC**, matching what the models store (`datetime.utcnow`). Anything
  timezone-aware is converted to UTC and stripped, so a peer on another timezone compares equal
  rather than appearing hours newer.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


def datetime_to_wire(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def datetime_from_wire(value: object) -> datetime | None:
    """Parse a wire timestamp to naive UTC. Unparseable reads as None, i.e. "unknown"."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def date_to_wire(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def date_from_wire(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def decimal_to_wire(value: Decimal | int | float | None) -> str | None:
    return str(value) if value is not None else None


def decimal_from_wire(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def bool_from_wire(value: object) -> bool:
    return bool(value)
