"""Causal revisions of provider observations, separate from user annotation clocks.

A peer may advance an already stored financial fact only along a verified chain from
that exact fact. Divergent observations are reported as conflicts, not resolved by a
wall clock or arbitrary monetary winner. Digests establish consistency/provenance,
not authentication; the sync transport is restricted to the owner's trusted devices.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

FIELDS = (
    'source', 'source_id', 'date', 'authorized_date', '_amount', 'currency',
    'merchant_raw', 'original_description', 'payment_channel', 'account_last4',
    'plaid_account_id', 'plaid_mask', 'pending', 'pending_transaction_id', 'origin',
)


def digest(values: dict) -> str:
    canonical = {}
    for field in FIELDS:
        value = values.get(field)
        if field == '_amount':
            value = format(Decimal(str(value or 0)).quantize(Decimal('0.01')), 'f')
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif field == 'pending':
            value = bool(value)
        canonical[field] = value
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def row_digest(row) -> str:
    return digest({field: getattr(row, field, None) for field in FIELDS})


def wire_digest(record: dict) -> str:
    return digest({field: record.get('amount' if field == '_amount' else field) for field in FIELDS})


def valid_revision(record: dict) -> bool:
    revision = record.get('provider_revision')
    if not isinstance(revision, dict) or revision.get('provider') != 'plaid' or record.get('origin') != 'plaid':
        return False
    ancestors = revision.get('ancestors')
    return (
        revision.get('digest') == wire_digest(record)
        and isinstance(ancestors, list)
        and all(isinstance(value, str) and len(value) == 64 for value in ancestors)
        and revision['digest'] not in ancestors
        and len(set(ancestors)) == len(ancestors)
    )


def observe(row, previous: str | None) -> None:
    """Called only by ingestion after a successful provider observation is applied."""
    current = row_digest(row)
    revision = row.provider_revision or {}
    ancestors = list(revision.get('ancestors') or [])
    if previous and previous != current and previous not in ancestors:
        ancestors.append(previous)
    row.provider_revision = {'provider': 'plaid', 'digest': current, 'ancestors': ancestors}
