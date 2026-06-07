"""Bank of America checking statement parser plugin."""
import os
import re
from datetime import datetime

import pdfplumber

from src.plugins.base import ParserPlugin


def _amt(s: str) -> float:
    return float(s.replace(",", "").replace("$", ""))


def _iso_date(year: int, month: str | int, day: str | int) -> str | None:
    from datetime import date

    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def parse_bofa(pdf_path: str) -> dict:
    pdf = pdfplumber.open(pdf_path)
    full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    def _parse_period_date(s: str) -> str | None:
        if not s:
            return None
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(s.strip(), fmt).date().isoformat()
            except ValueError:
                continue
        return None

    period_match = re.search(r"for (\w+ \d+, \d{4}) to (\w+ \d+, \d{4})", full_text)
    start_date = _parse_period_date(period_match.group(1)) if period_match else None
    end_date = _parse_period_date(period_match.group(2)) if period_match else None

    begin_match = re.search(r"Beginning balance.*?\$?([\d,]+\.\d{2})", full_text)
    end_match = re.search(r"Ending balance.*?\$?([\d,]+\.\d{2})", full_text)
    beginning_balance = _amt(begin_match.group(1)) if begin_match else None
    ending_balance = _amt(end_match.group(1)) if end_match else None

    fees_match = re.search(r"Service fees\s+-([\d,]+\.\d{2})", full_text)
    checks_match = re.search(r"Checks\s+-([\d,]+\.\d{2})", full_text)
    service_fees = _amt(fees_match.group(1)) if fees_match else 0
    checks_total = _amt(checks_match.group(1)) if checks_match else 0

    transactions = []

    checking_text = full_text
    savings_start = re.search(r"Your\s+.*Savings", full_text)
    if savings_start:
        checking_text = full_text[: savings_start.start()]

    sections = re.split(
        r"(Deposits and other additions|Withdrawals and other subtractions|Checks)\s*\n\s*Date\s+Description\s+Amount",
        checking_text,
    )

    for i in range(1, len(sections), 2):
        section_type = sections[i].strip()
        section_text = sections[i + 1] if i + 1 < len(sections) else ""
        is_withdrawal = "Withdrawal" in section_type or "Check" in section_type

        total_match = re.search(r"Total\s+(deposits|withdrawals|checks)", section_text)
        if total_match:
            section_text = section_text[: total_match.start()]

        current_txn = None
        for line in section_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            date_match = re.match(r"^(\d{2}/\d{2}/\d{2})\s+(.+)", line)
            if date_match:
                if current_txn:
                    transactions.append(current_txn)
                date_str = date_match.group(1)
                rest = date_match.group(2)
                amt_match = re.search(r"[- ]?([\d,]+\.\d{2})$", rest)
                amount = _amt(amt_match.group(1)) if amt_match else None
                desc = rest[: amt_match.start()].strip() if amt_match else rest
                if is_withdrawal and amount:
                    amount = -amount
                mm, dd, yy = date_str.split("/")
                iso_date = _iso_date(2000 + int(yy), mm, dd)
                if not iso_date:
                    current_txn = None
                    continue
                current_txn = {"date": iso_date, "description": desc, "amount": amount}
            elif current_txn:
                if current_txn["amount"] is None:
                    amt_match = re.search(r"[- ]?([\d,]+\.\d{2})$", line)
                    if amt_match:
                        amount = _amt(amt_match.group(1))
                        current_txn["amount"] = -amount if is_withdrawal else amount
                        extra = line[: amt_match.start()].strip()
                        if extra:
                            current_txn["description"] += " " + extra

        if current_txn:
            transactions.append(current_txn)

    if service_fees > 0:
        transactions.append(
            {
                "date": transactions[-1]["date"] if transactions else "",
                "description": "Service Fee",
                "amount": -service_fees,
            }
        )
    if checks_total > 0:
        transactions.append(
            {
                "date": transactions[-1]["date"] if transactions else "",
                "description": "Checks",
                "amount": -checks_total,
            }
        )

    pdf.close()
    return {
        "source": "Bank of America",
        "file": os.path.basename(pdf_path),
        "start_date": start_date,
        "end_date": end_date,
        "beginning_balance": beginning_balance,
        "ending_balance": ending_balance,
        "transactions": transactions,
        "transaction_count": len(transactions),
    }


def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="bofa",
        label="Bank of America",
        record_type="transactions",
        allowed_kinds={"statement_pdf"},
        domain="financial_accounts",
        directory_name="bofa",
        group="Bank Accounts",
        hint="Upload Bank of America checking statement PDF",
        currency="USD",
        statement_parser=parse_bofa,
        is_credit_card=False,
        icon_url="/api/plugin-icons/bofa.png",
        source_aliases=["Bank of America", "BofA", "BoA"],
    )
