"""American Express statement parser plugin."""
import os
import re

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


def parse_amex(pdf_path: str) -> dict:
    pdf = pdfplumber.open(pdf_path)
    full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    all_prev = [
        _amt(m.group(1))
        for m in re.finditer(r"Previous Balance\s+\$?([\d,]+\.\d{2})", full_text)
    ]
    prev_balance = all_prev[-1] if all_prev else 0

    all_pmt = [
        _amt(m.group(1))
        for m in re.finditer(r"Payments/Credits\s+-\$?([\d,]+\.\d{2})", full_text)
    ]
    all_chg = [
        _amt(m.group(1))
        for m in re.finditer(r"New Charges\s+\+\$?([\d,]+\.\d{2})", full_text)
    ]
    summary_pmt = all_pmt[-1] if all_pmt else 0
    summary_chg = all_chg[-1] if all_chg else 0

    nonplan_match = re.search(r"Non-Plan Balance\s*=\s*\$?([\d,]+\.\d{2})", full_text)
    new_match = re.search(r"New Balance\s+\$?([\d,]+\.\d{2})", full_text)
    ending_balance = (
        _amt(nonplan_match.group(1))
        if nonplan_match
        else (_amt(new_match.group(1)) if new_match else 0)
    )

    fees_match = re.search(r"Total Fees for this Period\s+\$?([\d,]+\.\d{2})", full_text)
    interest_match = re.search(
        r"Total Interest Charged for this Period\s+\$?([\d,]+\.\d{2})", full_text
    )
    fees = _amt(fees_match.group(1)) if fees_match else 0
    interest = _amt(interest_match.group(1)) if interest_match else 0

    close_match = re.search(r"Closing Date\s*(\d{2}/\d{2}/\d{2})", full_text)
    close_year = (
        2000 + int(close_match.group(1).split("/")[2])
        if close_match
        else int(re.search(r"20\d{2}", os.path.basename(pdf_path)).group())
    )
    cd_match = re.search(r"Closing Date\s*(\d{2}/\d{2}/\d{2})", full_text)
    close_date = (
        _iso_date(
            2000 + int(cd_match.group(1).split("/")[2]),
            cd_match.group(1).split("/")[0],
            cd_match.group(1).split("/")[1],
        )
        if cd_match
        else _iso_date(close_year, 1, 1)
    ) or f"{close_year}-01-01"

    plan_details_pos = re.search(r"Details?\s+For more details of your plans", full_text)
    fees_section = re.search(r"\nFees\nAmount\n", full_text)
    cutoff = min(
        plan_details_pos.start() if plan_details_pos else len(full_text),
        fees_section.start() if fees_section else len(full_text),
    )

    plan_descs = set()
    for m in re.finditer(
        r"Created\s+Description.*?\n(.*?)Plan Totals", full_text, re.DOTALL
    ):
        for line in m.group(1).split("\n"):
            pm = re.match(r"\d{2}/\d{2}/\d{2}\s+(.+?)\s+\d+\s+\$", line)
            if pm:
                plan_descs.add(tuple(pm.group(1).strip().split()[:3]))

    transactions = []
    for m in re.finditer(
        r"(\d{2}/\d{2}/\d{2})\*?\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$",
        full_text[:cutoff],
        re.MULTILINE,
    ):
        date_str, desc, amt_str = m.groups()
        if "Previous Balance" in desc:
            continue
        if tuple(desc.strip().split()[:3]) in plan_descs:
            continue
        mm, dd, yy = date_str.split("/")
        amount = _amt(amt_str.lstrip("-$"))
        if amt_str.startswith("-"):
            amount = -amount
        iso_date = _iso_date(2000 + int(yy), mm, dd)
        if not iso_date:
            continue
        transactions.append({"date": iso_date, "description": desc.strip(), "amount": amount})

    if fees > 0:
        transactions.append({"date": close_date, "description": "Fees", "amount": fees})
    if interest > 0:
        transactions.append(
            {"date": close_date, "description": "Interest", "amount": interest}
        )

    plan_bal_match = re.search(r"Plan Balance\s*=\s*\$?([\d,]+\.\d{2})", full_text)
    if plan_bal_match:
        txn_sum = sum(t["amount"] for t in transactions)
        expected_change = ending_balance - prev_balance
        residual = round(expected_change - txn_sum, 2)
        if abs(residual) > 0.01:
            transactions.append(
                {
                    "date": close_date,
                    "description": "Plan It Adjustment",
                    "amount": residual,
                }
            )

    pdf.close()
    return {
        "source": "American Express",
        "file": os.path.basename(pdf_path),
        "beginning_balance": prev_balance,
        "ending_balance": ending_balance,
        "transactions": transactions,
        "transaction_count": len(transactions),
    }


def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="amex",
        label="American Express",
        record_type="transactions",
        allowed_kinds={"statement_pdf"},
        domain="financial_accounts",
        directory_name="amex",
        group="Credit Cards",
        hint="Upload Amex statement PDF",
        currency="USD",
        statement_parser=parse_amex,
        is_credit_card=True,
        icon_url="/api/plugin-icons/amex.png",
        source_aliases=["American Express", "Amex", "AMEX"],
    )
