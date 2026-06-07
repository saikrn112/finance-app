"""Generic retirement transaction utilities (not tied to any specific provider)."""
from __future__ import annotations

from typing import Any


def summarize_retirement_transactions(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a list of retirement transactions into a balance/contribution overview."""
    if not transactions:
        return {}

    total_units = sum(float(txn["units"]) for txn in transactions)
    latest_price = float(transactions[-1]["unit_price"])
    balance = total_units * latest_price
    employee = sum(float(txn["amount"]) for txn in transactions if "Employee" in str(txn["source"]) and float(txn["amount"]) > 0)
    match = sum(float(txn["amount"]) for txn in transactions if "Match" in str(txn["source"]) and float(txn["amount"]) > 0)
    fees = sum(float(txn["amount"]) for txn in transactions if float(txn["amount"]) < 0)

    return {
        "balance": round(balance, 2),
        "total_units": round(total_units, 4),
        "unit_price": latest_price,
        "total_contributed": round(employee + match, 2),
        "employee_contributed": round(employee, 2),
        "employer_match": round(match, 2),
        "fees": round(fees, 2),
        "gain": round(balance - employee - match - fees, 2),
        "fund_name": str(transactions[0].get("fund") or ""),
        "date_range": [transactions[0]["date"], transactions[-1]["date"]],
    }
