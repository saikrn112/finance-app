from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import uuid

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, sessionmaker

from src.models import AppMetadata, PlaidApiUsage, PlaidProductEnrollment
from src.config import settings
from src.vault.backup import ensure_vault_metadata


PLAID_ENDPOINT_PRICING = {
    "accounts_balance_get": Decimal("0.10"),
    "investments_holdings_get": Decimal("0.00"),
    "investments_transactions_get": Decimal("0.00"),
    "transactions_item_month": Decimal("0.30"),
    "transactions_sync": Decimal("0.00"),
    "link_token_create": Decimal("0.00"),
    "item_public_token_exchange": Decimal("0.00"),
}


def _audit_session(db: Session) -> Session:
    return sessionmaker(bind=db.get_bind())()


def _database_instance_id(db: Session) -> str:
    row = db.get(AppMetadata, "database_instance_id")
    if row is None:
        row = AppMetadata(key="database_instance_id", value=str(uuid.uuid4()))
        db.add(row)
        db.flush()
    return row.value


def record_plaid_usage(
    db: Session,
    *,
    endpoint: str,
    institution: str | None = None,
    plaid_item_id: str | None = None,
    units: int | float = 1,
    metadata: dict | None = None,
) -> str:
    """Durably record a call independently from the caller's business transaction."""
    audit = _audit_session(db)
    try:
        details = dict(metadata or {})
        status = str(details.pop("status", "success"))
        vault = ensure_vault_metadata()
        device_id = str(vault.get("device_id") or "unknown")
        unit_cost = PLAID_ENDPOINT_PRICING.get(endpoint, Decimal("0.00"))
        usage = PlaidApiUsage(
            endpoint=endpoint,
            institution=institution,
            plaid_item_id=plaid_item_id,
            units=Decimal(str(units)),
            estimated_cost=unit_cost * Decimal(str(units)),
            status=status,
            metadata_json={
                "device_key": hashlib.sha256(device_id.encode()).hexdigest()[:12],
                "device_label": vault.get("device_label"),
                "environment": settings.app.mode,
                "database_id": _database_instance_id(audit),
                **details,
            },
        )
        audit.add(usage)
        audit.commit()
        return usage.id
    finally:
        audit.close()


def finish_plaid_usage(db: Session, usage_id: str, *, success: bool, error_code: str | None = None) -> None:
    audit = _audit_session(db)
    try:
        usage = audit.get(PlaidApiUsage, usage_id)
        if usage is not None:
            usage.status = "success" if success else "failed"
            if error_code:
                usage.metadata_json = {**(usage.metadata_json or {}), "error_code": error_code}
            audit.commit()
    finally:
        audit.close()


def upsert_product_enrollments(db: Session, plaid_item_id: str, products: list[str]) -> None:
    now = datetime.utcnow()
    requested = {product.strip() for product in products if product.strip()}
    existing = db.query(PlaidProductEnrollment).filter(
        PlaidProductEnrollment.plaid_item_id == plaid_item_id
    ).all()
    by_product = {row.product: row for row in existing}
    for product in requested:
        row = by_product.get(product)
        if row is None:
            db.add(PlaidProductEnrollment(plaid_item_id=plaid_item_id, product=product, active_from=now))
        else:
            row.active_until = None
    for product, row in by_product.items():
        if product not in requested and row.active_until is None:
            row.active_until = now


def deactivate_product_enrollments(db: Session, plaid_item_ids: list[str]) -> None:
    if not plaid_item_ids:
        return
    db.query(PlaidProductEnrollment).filter(
        PlaidProductEnrollment.plaid_item_id.in_(plaid_item_ids),
        PlaidProductEnrollment.active_until.is_(None),
    ).update({PlaidProductEnrollment.active_until: datetime.utcnow()}, synchronize_session=False)


def month_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def plaid_usage_summary(db: Session) -> dict:
    month_start, month_end = month_bounds_utc()
    rows = db.query(
        PlaidApiUsage.endpoint, func.count(PlaidApiUsage.id),
        func.sum(PlaidApiUsage.units), func.sum(PlaidApiUsage.estimated_cost),
    ).filter(
        PlaidApiUsage.created_at >= month_start,
        PlaidApiUsage.created_at < month_end,
    ).group_by(PlaidApiUsage.endpoint).order_by(PlaidApiUsage.endpoint.asc()).all()
    transaction_items = db.query(PlaidProductEnrollment).filter(
        PlaidProductEnrollment.product == "transactions",
        PlaidProductEnrollment.active_from < month_end,
        or_(PlaidProductEnrollment.active_until.is_(None), PlaidProductEnrollment.active_until >= month_start),
    ).count()
    balance_calls = next((int(row[1] or 0) for row in rows if row[0] == "accounts_balance_get"), 0)
    billing_lines = [
        {"label": "Transactions usage", "quantity": transaction_items, "unit": "active items", "unit_price": 0.30, "estimated_cost": round(transaction_items * 0.30, 2)},
        {"label": "Balance usage", "quantity": balance_calls, "unit": "local calls", "unit_price": 0.10, "estimated_cost": round(balance_calls * 0.10, 2)},
    ]
    usage_rows = db.query(PlaidApiUsage).filter(
        PlaidApiUsage.created_at >= month_start, PlaidApiUsage.created_at < month_end
    ).all()
    devices: dict[str, dict] = {}
    for usage in usage_rows:
        metadata = usage.metadata_json or {}
        device_key = metadata.get("device_key") or "legacy-unattributed"
        device = devices.setdefault(device_key, {
            "device_key": device_key, "device_label": metadata.get("device_label") or "Legacy / unattributed",
            "environment": metadata.get("environment"), "database_id": metadata.get("database_id"),
            "call_count": 0, "balance_calls": 0, "endpoints": {},
        })
        device["call_count"] += 1
        if usage.endpoint == "accounts_balance_get":
            device["balance_calls"] += 1
        device["endpoints"][usage.endpoint] = device["endpoints"].get(usage.endpoint, 0) + 1
    return {
        "month_start": month_start.date().isoformat(), "month_end_exclusive": month_end.date().isoformat(),
        "total_estimated_cost": round(sum(row["estimated_cost"] for row in billing_lines), 2),
        "billing_lines": billing_lines, "scope": "local_instance",
        "scope_note": "Plaid invoices aggregate all environments sharing these credentials; local calls may be lower.",
        "devices": sorted(devices.values(), key=lambda item: (-item["call_count"], item["device_key"])),
        "endpoints": [{
            "endpoint": row[0], "call_count": int(row[1] or 0), "units": float(row[2] or 0),
            "estimated_cost": round(float(row[2] or 0) * float(PLAID_ENDPOINT_PRICING.get(row[0], Decimal("0.00"))), 2),
        } for row in rows],
    }
