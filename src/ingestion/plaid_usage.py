from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models import PlaidApiUsage, SyncLog
from src.config import settings
from src.plugins.registry import classify_source, get_all_sources
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


def record_plaid_usage(
    db: Session,
    *,
    endpoint: str,
    institution: str | None = None,
    plaid_item_id: str | None = None,
    units: int | float = 1,
    metadata: dict | None = None,
) -> None:
    unit_cost = PLAID_ENDPOINT_PRICING.get(endpoint, Decimal("0.00"))
    vault_metadata = ensure_vault_metadata()
    runtime_environment = Path(settings.app.runtime_dir).name or settings.app.mode
    database_name = Path(settings.database.path).name
    database_id = hashlib.sha256(
        f"{vault_metadata.get('device_id')}|{runtime_environment}|{settings.database.path}".encode()
    ).hexdigest()[:12]
    attribution = {
        "device_id": vault_metadata.get("device_id"),
        "device_label": vault_metadata.get("device_label"),
        "environment": runtime_environment,
        "database_id": database_id,
        "database_name": database_name,
    }
    usage = PlaidApiUsage(
        endpoint=endpoint,
        institution=institution,
        plaid_item_id=plaid_item_id,
        units=Decimal(str(units)),
        estimated_cost=unit_cost * Decimal(str(units)),
        metadata_json={**attribution, **(metadata or {})},
    )
    db.add(usage)


def month_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def plaid_usage_summary(db: Session) -> dict:
    month_start, month_end = month_bounds_utc()
    rows = (
        db.query(
            PlaidApiUsage.endpoint,
            func.count(PlaidApiUsage.id),
            func.sum(PlaidApiUsage.units),
            func.sum(PlaidApiUsage.estimated_cost),
        )
        .filter(PlaidApiUsage.created_at >= month_start, PlaidApiUsage.created_at < month_end)
        .group_by(PlaidApiUsage.endpoint)
        .order_by(PlaidApiUsage.endpoint.asc())
        .all()
    )
    connected_logs = (
        db.query(SyncLog)
        .filter(SyncLog.sync_type == "plaid", SyncLog.status == "connected")
        .all()
    )
    transaction_items = 0
    for log in connected_logs:
        institution = (log.extra_data or {}).get("institution_name", "")
        plugin = get_all_sources().get(classify_source(institution))
        if not plugin or plugin.domain not in {"investments", "retirement"}:
            transaction_items += 1

    balance_calls = next((int(row[1] or 0) for row in rows if row[0] == "accounts_balance_get"), 0)
    billing_lines = [
        {
            "label": "Transactions usage",
            "quantity": transaction_items,
            "unit": "active items",
            "unit_price": 0.30,
            "estimated_cost": round(transaction_items * 0.30, 2),
        },
        {
            "label": "Balance usage",
            "quantity": balance_calls,
            "unit": "local calls",
            "unit_price": 0.10,
            "estimated_cost": round(balance_calls * 0.10, 2),
        },
    ]
    usage_rows = (
        db.query(PlaidApiUsage)
        .filter(PlaidApiUsage.created_at >= month_start, PlaidApiUsage.created_at < month_end)
        .all()
    )
    devices: dict[str, dict] = {}
    for usage in usage_rows:
        metadata = usage.metadata_json or {}
        device_id = metadata.get("device_id") or "legacy-unattributed"
        device = devices.setdefault(device_id, {
            "device_key": hashlib.sha256(device_id.encode()).hexdigest()[:12] if device_id != "legacy-unattributed" else device_id,
            "device_label": metadata.get("device_label") or "Legacy / unattributed",
            "environment": metadata.get("environment"),
            "database_id": metadata.get("database_id"),
            "database_name": metadata.get("database_name"),
            "call_count": 0,
            "balance_calls": 0,
            "endpoints": {},
        })
        device["call_count"] += 1
        if usage.endpoint == "accounts_balance_get":
            device["balance_calls"] += 1
        device["endpoints"][usage.endpoint] = device["endpoints"].get(usage.endpoint, 0) + 1
    device_rows = sorted(devices.values(), key=lambda item: (-item["call_count"], item["device_key"]))
    return {
        "month_start": month_start.date().isoformat(),
        "month_end_exclusive": month_end.date().isoformat(),
        "total_estimated_cost": round(sum(row["estimated_cost"] for row in billing_lines), 2),
        "billing_lines": billing_lines,
        "scope": "local_instance",
        "scope_note": "Plaid invoices aggregate all environments sharing these credentials; local calls may be lower.",
        "devices": device_rows,
        "endpoints": [
            {
                "endpoint": row[0],
                "call_count": int(row[1] or 0),
                "units": float(row[2] or 0),
                "estimated_cost": round(
                    float(row[2] or 0) * float(PLAID_ENDPOINT_PRICING.get(row[0], Decimal("0.00"))),
                    2,
                ),
            }
            for row in rows
        ],
    }
