"""Currency conversion service — the single source of truth for rate conversion.

Architecture:
- Processing modules are currency-agnostic (work with raw amounts).
- API routes call get_rate_map() once per request and convert at the serialization boundary.
- SQL aggregation paths use latest_rate_subquery() JOIN directly.

Usage in API routes:
    from src.services.currency import get_rate_map, convert

    rate_map = get_rate_map(db, currency)
    converted = convert(raw_amount, txn_currency, rate_map)
"""

from sqlalchemy.orm import Session

from src.services.exchange_rates import ensure_rates_fresh, latest_rate_subquery


def get_rate_map(db: Session, target_currency: str) -> dict[str, float]:
    """Build a {from_currency: rate} lookup for converting TO target_currency.

    Call once per request; pass the result to convert() or serialization helpers.
    Ensures rates are fresh before building the map.
    """
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rows = (
        db.query(lr.c.from_currency, lr.c.rate)
        .filter(lr.c.to_currency == target_currency)
        .all()
    )
    return {row[0]: float(row[1]) for row in rows}


def convert(raw_amount: float, from_currency: str, rate_map: dict[str, float]) -> float:
    """Convert a single raw amount to target currency using the rate_map.

    Returns 0.0 if the currency has no rate (JOIN semantics — missing rate = excluded).
    """
    rate = rate_map.get(from_currency)
    if rate is None:
        return 0.0
    return raw_amount * rate
