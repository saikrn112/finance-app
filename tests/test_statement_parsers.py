import plugins.statements as parse_statements


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, text: str):
        self.pages = [_FakePage(text)]

    def close(self):
        return None


def test_parse_discover_resolves_year_boundary_from_statement_period(monkeypatch):
    text = """
OPEN TO CLOSE DATE: 12/19/2022 - 01/18/2023
Previous Balance -$57.01
New Balance: $490.56
Payments and Credits -$57.01
Purchases +$547.57
12/26 AMAZON.COM*AE21B4XS3 Merchandise $12.74
01/07 WALMART.COM 8009666546 BENTONVILLE AR -$2.67
""".strip()

    monkeypatch.setattr(parse_statements.pdfplumber, "open", lambda _: _FakePdf(text))

    result = parse_statements.parse_discover("Discover-Statement-20230118-4238.pdf")

    assert result["transactions"][0]["date"] == "2022-12-26"
    assert result["transactions"][1]["date"] == "2023-01-07"


def test_parse_bofa_skips_invalid_dates_instead_of_raising(monkeypatch):
    text = """
for October 1, 2025 to October 31, 2025
Beginning balance $100.00
Ending balance $150.00
Deposits and other additions
Date Description Amount
40/04/25 BAD OCR DATE 10.00
10/04/25 VALID PAYROLL 50.00
""".strip()

    monkeypatch.setattr(parse_statements.pdfplumber, "open", lambda _: _FakePdf(text))

    result = parse_statements.parse_bofa("eStmt_2025-10-31.pdf")

    assert result["transaction_count"] == 1
    assert result["transactions"][0]["date"] == "2025-10-04"
    assert result["transactions"][0]["description"] == "VALID PAYROLL"


def test_parse_apple_reads_total_balance_layout(monkeypatch):
    text = """
Previous Monthly Balance $36.13
as of Jul 31, 2025
Total Balance $384.44
as of Aug 31, 2025
Payments
Date Description Amount
08/17/2025 ACH Deposit Internet transfer from account ending in 1907 -$36.13
Total payments for this period -$36.13
Transactions
Date Description Daily Cash Amount
08/27/2025 APPLE STORE #R168 10300 Little Patuxent Pk COLUMBIA 21044 MD USA 3% $1.56 $51.94
Total Daily Cash this month $1.56
Total charges, credits and returns $51.94
Apple Card Monthly Installments
Dates Description Daily Cash Amounts
06/04/2025 Apple Online Store Cupertino CA $399.00
TRANSACTION #bd3bd91d2e1a
This month's installment: $33.25
Final installment: Jun 30, 2026
""".strip()

    monkeypatch.setattr(parse_statements.pdfplumber, "open", lambda _: _FakePdf(text))

    result = parse_statements.parse_apple("Apple Card Statement - August 2025.pdf")

    assert result["ending_balance"] == 384.44
    assert result["transaction_count"] == 3
    assert result["transactions"][0]["date"] == "2025-08-17"
