from __future__ import annotations

from typing import Any, Callable

from src.plugins.base import CsvColumnConfig, ParserPlugin
from src.plugins.loader import get_registry


def _by_key() -> dict[str, ParserPlugin]:
    return {p.source_key: p for p in get_registry()}


def get_all_sources() -> dict[str, ParserPlugin]:
    """Return all registered plugins keyed by source_key."""
    return _by_key()


def get_source_label(source: str) -> str:
    """Return canonical display label for a source name. Falls back to input if no plugin matches."""
    key = classify_source(source)
    plugin = _by_key().get(key)
    return plugin.label if plugin else source


def get_import_source_defs() -> dict[str, dict[str, Any]]:
    """Produce a dict compatible with the legacy IMPORT_SOURCE_DEFS structure."""
    result: dict[str, dict[str, Any]] = {}
    for p in get_registry():
        result[p.source_key] = {
            "label": p.label,
            "record_type": p.record_type,
            "allowed_kinds": p.allowed_kinds,
        }
    return result


def get_source_raw_locations() -> dict[str, tuple[str, str]]:
    """Produce a dict compatible with the legacy SOURCE_RAW_LOCATIONS structure."""
    return {p.source_key: (p.domain, p.directory_name) for p in get_registry()}


def get_statement_parser(source: str) -> Callable | None:
    """Return the statement parser for a given source key."""
    plugins = _by_key()
    plugin = plugins.get(source)
    if plugin:
        return plugin.statement_parser
    return None


def get_payslip_parser(source: str) -> Callable | None:
    """Return the payslip parser for a given source key."""
    plugins = _by_key()
    plugin = plugins.get(source)
    if plugin:
        return plugin.payslip_parser
    return None


def get_retirement_parser(source: str) -> Callable | None:
    """Return the retirement parser for a given source key."""
    plugins = _by_key()
    plugin = plugins.get(source)
    if plugin:
        return plugin.retirement_parser
    return None


def get_investment_parser(source: str) -> Callable | None:
    """Return the investment parser for a given source key."""
    plugins = _by_key()
    plugin = plugins.get(source)
    if plugin:
        return plugin.investment_parser
    return None


def get_csv_config(source: str) -> CsvColumnConfig | None:
    """Return the CSV column config for a given source key."""
    plugins = _by_key()
    plugin = plugins.get(source)
    if plugin:
        return plugin.csv_config
    return None


def get_credit_card_sources() -> set[str]:
    """Return the set of source labels that are credit cards."""
    return {p.label for p in get_registry() if p.is_credit_card}


def get_source_options_for_frontend() -> list[dict]:
    """Return source options formatted for the frontend dropdown."""
    options = []
    for p in get_registry():
        options.append({
            "value": p.source_key,
            "label": p.label,
            "record_type": p.record_type,
            "allowed_kinds": sorted(p.allowed_kinds),
            "group": p.group,
            "hint": p.hint,
        })
    return options


def classify_source(source_name: str) -> str:
    """Match a source name (label or alias) to its source_key."""
    normalized = source_name.strip().lower()
    for p in get_registry():
        if p.label.lower() == normalized:
            return p.source_key
        if p.source_key.lower() == normalized:
            return p.source_key
        for alias in p.source_aliases:
            if alias.lower() == normalized:
                return p.source_key
    return source_name
