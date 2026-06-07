"""HDFC Bank statement PDF parser plugin."""
import os
import re
from datetime import date

import pdfplumber

from src.plugins.base import ParserPlugin


def _amt(s: str) -> float:
    return float(s.replace(",", "").replace("₹", "").strip())


def _parse_date(s: str) -> str | None:
    try:
        dd, mm, yyyy = s.strip().split("/")
        return date(int(yyyy), int(mm), int(dd)).isoformat()
    except (ValueError, AttributeError):
        return None


def parse_hdfc(pdf_path: str) -> dict:
    pdf = pdfplumber.open(pdf_path)
    pages_text = [p.extract_text() or "" for p in pdf.pages]
    full_text = "\n".join(pages_text)

    all_transactions = []
    opening_balance = None
    closing_balance = None
    period_start = None
    period_end = None

    account_sections = re.split(
        r"(?=Customer ID\s*:\s*\d+\s*\n.*?Account Number)", full_text
    )
    if len(account_sections) <= 1:
        account_sections = [full_text]

    for section in account_sections:
        summary_match = re.search(
            r"Opening Balance.*?Debit Amount.*?Credit Amount.*?Closing Balance\s*\n"
            r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})",
            section,
            re.DOTALL,
        )
        if not summary_match:
            continue

        sec_opening = _amt(summary_match.group(1))
        sec_debit = _amt(summary_match.group(2))
        sec_credit = _amt(summary_match.group(3))
        sec_closing = _amt(summary_match.group(4))

        if sec_debit == 0 and sec_credit == 0:
            continue

        if opening_balance is None:
            opening_balance = sec_opening
            closing_balance = sec_closing

        period_match = re.search(
            r"Statement From\s*:\s*(\d{2}/\d{2}/\d{4})\s*To\s*(\d{2}/\d{2}/\d{4})",
            section,
        )
        if period_match and period_start is None:
            period_start = _parse_date(period_match.group(1))
            period_end = _parse_date(period_match.group(2))

        txn_header = re.search(
            r"Txn Date\s+Narration\s+Withdrawals?\s+Deposits?\s+Closing Balance",
            section,
        )
        if not txn_header:
            continue

        txn_block = section[txn_header.end():]
        summary_pos = re.search(r"\bSUMMARY\b", txn_block)
        if summary_pos:
            txn_block = txn_block[: summary_pos.start()]

        lines = txn_block.strip().split("\n")
        current_txn = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            date_match = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.+)", line)
            if date_match:
                if current_txn:
                    all_transactions.append(current_txn)

                txn_date = _parse_date(date_match.group(1))
                rest = date_match.group(2)

                tail_match = re.search(
                    r"\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$",
                    rest,
                )
                if tail_match:
                    withdrawal = _amt(tail_match.group(1))
                    deposit = _amt(tail_match.group(2))
                    narration = rest[: tail_match.start()].strip()
                    amount = deposit if deposit > 0 else -withdrawal
                    current_txn = {
                        "date": txn_date,
                        "description": narration,
                        "amount": amount,
                    }
                else:
                    current_txn = {
                        "date": txn_date,
                        "description": rest.strip(),
                        "amount": None,
                    }
            elif current_txn:
                tail_match = re.search(
                    r"\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$",
                    line,
                )
                if tail_match and current_txn["amount"] is None:
                    withdrawal = _amt(tail_match.group(1))
                    deposit = _amt(tail_match.group(2))
                    current_txn["amount"] = deposit if deposit > 0 else -withdrawal
                    desc_part = line[: tail_match.start()].strip()
                    if desc_part:
                        current_txn["description"] += " " + desc_part
                else:
                    if not re.match(r"^[\d,]+\.\d{2}$", line):
                        current_txn["description"] += " " + line

        if current_txn:
            all_transactions.append(current_txn)

    for txn in all_transactions:
        desc = txn["description"]
        desc = re.sub(r"\s+", " ", desc).strip()
        value_dt_match = re.search(r"\s*Value Dt\s+\d{2}/\d{2}/\d{4}", desc)
        if value_dt_match:
            desc = desc[: value_dt_match.start()]
        ref_match = re.search(r"\s*Ref\s+\d+\s*$", desc)
        if ref_match:
            desc = desc[: ref_match.start()]
        txn["description"] = desc.strip()

    if opening_balance is None:
        opening_match = re.search(r"Opening Balance\s*:?\s*([\d,]+\.\d{2})", full_text)
        opening_balance = _amt(opening_match.group(1)) if opening_match else 0
    if closing_balance is None:
        closing_match = re.search(r"Closing Balance\s*:?\s*([\d,]+\.\d{2})", full_text)
        closing_balance = _amt(closing_match.group(1)) if closing_match else 0

    txn_sum = sum(t["amount"] for t in all_transactions if t["amount"] is not None)
    computed_ending = round(opening_balance + txn_sum, 2)

    pdf.close()
    return {
        "source": "HDFC Bank",
        "file": os.path.basename(pdf_path),
        "start_date": period_start,
        "end_date": period_end,
        "beginning_balance": opening_balance,
        "ending_balance": closing_balance,
        "transactions": all_transactions,
        "transaction_count": len(all_transactions),
        "computed_ending": computed_ending,
    }


def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="hdfc",
        label="HDFC Bank",
        record_type="transactions",
        allowed_kinds={"statement_pdf"},
        domain="financial_accounts",
        directory_name="hdfc",
        group="Bank Accounts",
        hint="Upload HDFC Bank monthly statement PDF",
        currency="INR",
        statement_parser=parse_hdfc,
        is_credit_card=False,
        icon_url="/api/plugin-icons/hdfc.png",
        source_aliases=["HDFC", "HDFC Bank", "hdfc bank"],
        filename_patterns=["*.pdf"],
    )
