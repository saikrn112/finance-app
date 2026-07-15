"""Chase credit card statement parser plugin."""
import os
import re

import pdfplumber

from src.plugins.base import CsvColumnConfig, ParserPlugin


def _amt(s: str) -> float:
    return float(s.replace(",", "").replace("$", ""))


def _iso_date(year: int, month: str | int, day: str | int) -> str | None:
    from datetime import date

    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def parse_chase(pdf_path: str) -> dict:
    pdf = pdfplumber.open(pdf_path)
    full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    prev_match = re.search(r"Previous [Bb]alance\s+\$?([\d,]+\.\d{2})", full_text)
    new_match = re.search(r"New [Bb]alance\s+\$?([\d,]+\.\d{2})", full_text)
    prev_balance = _amt(prev_match.group(1)) if prev_match else 0
    new_balance = _amt(new_match.group(1)) if new_match else 0

    # Chase year from filename: 20231116-statements-7521-.pdf
    fname = os.path.basename(pdf_path)
    year_match = re.match(r"(\d{4})", fname)
    default_year = int(year_match.group(1)) if year_match else 2024

    transactions = []
    # Chase format: MM/DD DESCRIPTION AMOUNT (positive = charge, negative = credit)
    txn_pattern = re.compile(r"(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})$", re.MULTILINE)

    for m in txn_pattern.finditer(full_text):
        date_str, desc, amt_str = m.groups()
        mm, dd = date_str.split("/")
        month = int(mm)
        # Determine year from closing date context
        year = default_year if month <= 12 else default_year
        # Handle year boundary (Dec statement might have Nov transactions)
        if month == 12 and "01" in fname[4:6]:
            year = default_year - 1
        amount = _amt(amt_str)
        iso_date = _iso_date(year, mm, dd)
        if not iso_date:
            continue
        transactions.append({"date": iso_date, "description": desc.strip(), "amount": amount})

    pdf.close()
    return {
        "source": "Chase",
        "file": fname,
        "beginning_balance": prev_balance,
        "ending_balance": new_balance,
        "transactions": transactions,
        "transaction_count": len(transactions),
    }


def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="chase",
        label="Chase",
        record_type="transactions",
        allowed_kinds={"csv", "statement_pdf"},
        domain="financial_accounts",
        directory_name="chase",
        group="Credit Cards",
        hint="Upload CSV export or statement PDF",
        currency="USD",
        statement_parser=parse_chase,
        csv_config=CsvColumnConfig(
            date_col="Transaction Date",
            amount_col="Amount",
            merchant_col="Description",
            date_fmt="%m/%d/%Y",
        ),
        is_credit_card=True,
        icon_url="/api/plugin-icons/chase.png",
        source_aliases=["Chase"],
    )
