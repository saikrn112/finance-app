# Parser Plugins

This directory contains parser plugins for the finance app. Each `.py` file must export a `register()` function that returns a `ParserPlugin` or `list[ParserPlugin]`.

Plugins are gitignored — your personal parsers stay private.

## Quick Start

```python
from src.plugins.base import ParserPlugin, CsvColumnConfig

def parse_my_bank(file_path: str) -> dict:
    # Your parsing logic here
    return {
        "source": "My Bank",
        "transactions": [
            {"date": "2026-01-15", "description": "GROCERY STORE", "amount": -42.50},
            {"date": "2026-01-16", "description": "PAYROLL DEPOSIT", "amount": 3200.00},
        ],
        "beginning_balance": 5000.00,
        "ending_balance": 5157.50,
    }

def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="my_bank",
        label="My Bank",
        record_type="transactions",
        allowed_kinds={"csv", "statement_pdf"},
        domain="financial_accounts",
        directory_name="my_bank",
        group="Bank Accounts",
        hint="Upload CSV or statement PDF",
        currency="USD",
        statement_parser=parse_my_bank,
        csv_config=CsvColumnConfig(
            date_col="Date",
            amount_col="Amount",
            merchant_col="Description",
        ),
        is_credit_card=False,
        source_aliases=["My Bank", "mybank"],
    )
```

## Plugin Discovery

Plugins are loaded from (in order):
1. `<repo>/plugins/` directory
2. `~/.finance-app/plugins/`
3. `$FINANCE_PLUGINS_DIR` environment variable

Files starting with `_` are ignored. Broken plugins are skipped with a warning logged.

---

## ParserPlugin Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_key` | `str` | Yes | Unique machine identifier (e.g., `"example_bank"`) |
| `label` | `str` | Yes | Human-readable name shown in UI |
| `record_type` | `str` | Yes | One of: `"transactions"`, `"payslip"`, `"retirement"`, `"investment"` |
| `allowed_kinds` | `set[str]` | Yes | File types accepted: `"csv"`, `"statement_pdf"`, `"payslip_pdf"`, `"retirement_csv"`, `"retirement_statement_pdf"`, `"investment_csv"` |
| `domain` | `str` | Yes | Storage domain: `"financial_accounts"`, `"investments"`, `"retirement"`, `"payroll"` |
| `directory_name` | `str` | Yes | Subdirectory name under `data/raw/<domain>/` |
| `group` | `str` | No | Frontend grouping in import dropdown (e.g., `"Bank Accounts"`, `"Credit Cards"`, `"Retirement"`) |
| `hint` | `str` | No | Help text shown in import UI |
| `currency` | `str` | No | ISO 4217 currency code (default: `"USD"`). Used to tag imported transactions. |
| `icon_url` | `str` | No | URL to icon (e.g., `"/api/plugin-icons/mybank.png"` for local icons in `plugins/icons/`) |
| `statement_parser` | `callable` | No | Parser for PDF/file statements |
| `payslip_parser` | `callable` | No | Parser for payslip PDFs |
| `retirement_parser` | `callable` | No | Parser for retirement CSV/files |
| `investment_parser` | `callable` | No | Parser for investment CSV/files |
| `csv_config` | `CsvColumnConfig` | No | Column mapping for generic CSV import |
| `is_credit_card` | `bool` | No | If true, amounts are treated as liabilities in net worth |
| `source_aliases` | `list[str]` | No | Alternative names that match this source in existing DB data |
| `filename_patterns` | `list[str]` | No | Glob patterns for file type hints |

---

## Record Types & Expected Return Shapes

### `record_type = "transactions"` (Banks, Credit Cards)

Used for: bank statements, credit card statements, CSV exports

**Parser function signature:**
```python
def my_parser(file_path: str) -> dict:
```

**Expected return:**
```python
{
    "source": "My Bank",                    # Institution name
    "file": "statement-2026-01.pdf",        # Original filename (optional)
    "start_date": "January 1, 2026",        # Statement period start (optional)
    "end_date": "January 31, 2026",         # Statement period end (optional)
    "beginning_balance": 5000.00,           # Opening balance (optional)
    "ending_balance": 5157.50,              # Closing balance (optional)
    "transactions": [                       # Required — list of transactions
        {
            "date": "2026-01-15",           # ISO date string (YYYY-MM-DD)
            "description": "GROCERY STORE", # Merchant/description as it appears
            "amount": -42.50,               # Negative = expense, Positive = income/credit
        },
    ],
    "transaction_count": 25,                # Optional — count for validation
}
```

**Key rules:**
- `amount`: negative for money going out (purchases, fees), positive for money coming in (deposits, credits, refunds)
- For credit cards: charges are negative, payments/credits are positive
- `date`: must be ISO format `YYYY-MM-DD`

**CSV alternative** — instead of a `statement_parser`, provide a `csv_config`:
```python
csv_config=CsvColumnConfig(
    date_col="Transaction Date",    # Column header for date
    amount_col="Amount",            # Column header for amount
    merchant_col="Description",     # Column header for merchant/description
    date_fmt="%m/%d/%Y",            # strptime format for parsing dates
    negate_amount=False,            # If true, multiply amount by -1
)
```

---

### `record_type = "payslip"` (Payroll)

Used for: payslip PDFs, pay statements

**Parser function signature:**
```python
def my_parser(file_path: str) -> dict | list[dict] | None:
```

**Expected return (single payslip):**
```python
{
    "employer": "Acme Corp",
    "pay_date": "2026-01-15",               # or "01/15/2026" — both work
    "period_start": "2026-01-01",           # Optional
    "period_end": "2026-01-14",             # Optional
    "gross": 6500.00,
    "net": 4200.00,
    "total_taxes": 1500.00,
    "total_deductions": 800.00,
    "taxes": {                              # Optional breakdown
        "federal": 900.00,
        "state_tax": 350.00,
        "social_security": 180.00,
        "medicare": 70.00,
    },
    "deductions": {                         # Optional breakdown
        "401k": 500.00,
        "medical": 200.00,
        "dental": 50.00,
        "hsa": 50.00,
    },
    "earnings": {                           # Optional breakdown
        "regular": 6000.00,
        "bonus": 500.00,
    },
    "hours": 80.0,                          # Optional
    "gross_ytd": 13000.00,                  # Optional
    "filename": "paystub-2026-01-15.pdf",   # Optional, for display
    "folder": "acme_payroll",               # Optional, for dedup
}
```

**For multi-payslip PDFs** (one file contains multiple pay periods), return a `list[dict]`.

**Return `None`** if the file could not be parsed.

---

### `record_type = "retirement"` (401k, Pension, Provident Fund)

Used for: retirement account CSVs, 401k statements

**Parser function signature:**
```python
def my_parser(file_path: str) -> dict:
```

**Expected return (transaction-based, e.g., contribution/withdrawal history):**
```python
{
    "transactions": [
        {
            "source_id": "unique-hash-per-row",     # For dedup — hash of key fields
            "type": "Contribution",                 # Transaction type
            "date": "2026-01-15",                   # ISO date
            "source": "Employee",                   # Source of funds
            "fund": "Example Target Fund",           # Fund/investment name
            "units": 12.5,                          # Units purchased
            "unit_price": 45.20,                    # Price per unit
            "amount": 565.00,                       # Dollar amount
        },
    ],
    "summary": {                                    # Optional — auto-computed if omitted
        "balance": 45000.00,
        "total_contributed": 30000.00,
        "gain": 15000.00,
    },
}
```

**Expected return (statement-based retirement account):**
```python
{
    "source_type": "example_retirement",            # Identifier for this source
    "plan_name": "401k Savings Plan",               # Optional
    "statements": [                                 # Periodic balance statements
        {
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "beginning_balance": 40000.00,
            "ending_balance": 45000.00,
            "employee_contributions": 3000.00,
            "employer_contributions": 1500.00,
            "market_change": 500.00,
            "vested_balance": 44000.00,
            "rate_of_return": 0.035,
        },
    ],
    "activity": [                                   # Optional — individual transactions
        {
            "date": "2026-02-15",
            "investment": "Example Target Fund",
            "transaction_type": "Contribution",
            "amount": 1500.00,
            "shares": 33.2,
        },
    ],
    "transactions": [                               # Normalized transactions for DB
        {
            "source_id": "unique-hash",
            "type": "Statement",
            "date": "2026-03-31",
            "source": "Statement Summary",
            "fund": "Total",
            "units": 0.0,
            "unit_price": 0.0,
            "amount": 45000.00,
        },
    ],
}
```

---

### `record_type = "investment"` (Brokerage, HSA, Trading)

Used for: investment account CSVs, holdings snapshots

**Parser function signature:**
```python
def my_parser(file_path: str) -> dict:
```

**Expected return:**
```python
{
    "account": {                                    # Account metadata
        "account_number": "****1234",               # Masked or partial
        "account_type": "HSA",                      # or "Brokerage", "Trading"
        "statement_date": "2026-03-31",
    },
    "holdings": [                                   # Current positions
        {
            "security_id": "unique-hash",           # For dedup
            "symbol": "VTI",                        # Ticker symbol
            "description": "Example Broad Market",
            "quantity": 50.0,
            "price": 245.30,
            "value": 12265.00,
            "cost_basis": 10000.00,                 # Optional
        },
    ],
    "summary": {
        "total_value": 12265.00,
        "statement_date": "2026-03-31",
        "account_number": "****1234",
    },
}
```

---

## Complete Examples

### Bank Account — CSV Import (non-USD currency)

```python
from src.plugins.base import ParserPlugin, CsvColumnConfig

def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="my_bank",
        label="My Bank",
        record_type="transactions",
        allowed_kinds={"csv"},
        domain="financial_accounts",
        directory_name="my_bank",
        group="Bank Accounts",
        hint="Export CSV from online banking",
        currency="INR",  # or "USD", "EUR", "GBP", etc.
        csv_config=CsvColumnConfig(
            date_col="Date",
            amount_col="Amount",
            merchant_col="Narration",
            date_fmt="%d/%m/%y",
            negate_amount=True,
        ),
        source_aliases=["My Bank"],
    )
```

### Retirement Account — Custom CSV Parser

```python
import csv
import hashlib
from pathlib import Path
from src.plugins.base import ParserPlugin

def parse_retirement(file_path: str) -> dict:
    path = Path(file_path)
    transactions = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            date_raw = row.get("Date", "").strip()
            amount = float(row.get("Amount", "0").replace(",", ""))
            tx_type = row.get("Type", "Contribution")
            source_id = hashlib.sha1(
                f"{date_raw}|{tx_type}|{amount}".encode()
            ).hexdigest()[:32]
            transactions.append({
                "source_id": source_id,
                "type": tx_type,
                "date": date_raw,  # Must be YYYY-MM-DD
                "source": "Employee" if "employee" in tx_type.lower() else "Employer",
                "fund": "Default Fund",
                "units": 0,
                "unit_price": 0,
                "amount": amount,
            })
    return {"transactions": transactions}

def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="my_retirement",
        label="My Retirement Plan",
        record_type="retirement",
        allowed_kinds={"retirement_csv"},
        domain="retirement",
        directory_name="my_retirement",
        group="Retirement",
        hint="Upload retirement account CSV export",
        currency="USD",
        retirement_parser=parse_retirement,
        source_aliases=["My Retirement"],
    )
```

### Investment/Brokerage — Holdings CSV

```python
import csv
import hashlib
from pathlib import Path
from src.plugins.base import ParserPlugin

def parse_holdings(file_path: str) -> dict:
    path = Path(file_path)
    holdings = []
    total_value = 0
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            symbol = row.get("Symbol", "").strip()
            qty = float(row.get("Qty", "0"))
            price = float(row.get("Price", "0").replace(",", ""))
            value = round(qty * price, 2)
            total_value += value
            security_id = hashlib.sha1(f"my_broker|{symbol}".encode()).hexdigest()[:32]
            holdings.append({
                "security_id": security_id,
                "symbol": symbol,
                "description": row.get("Name", symbol),
                "quantity": qty,
                "price": price,
                "value": value,
                "cost_basis": float(row.get("Avg Cost", "0").replace(",", "")) * qty,
            })
    return {
        "account": {"account_type": "Brokerage"},
        "holdings": holdings,
        "summary": {"total_value": round(total_value, 2)},
    }

def register() -> ParserPlugin:
    return ParserPlugin(
        source_key="my_broker",
        label="My Broker",
        record_type="investment",
        allowed_kinds={"investment_csv"},
        domain="investments",
        directory_name="my_broker",
        group="Investments",
        hint="Upload holdings CSV from your brokerage",
        currency="USD",
        investment_parser=parse_holdings,
        source_aliases=["My Broker"],
    )
```

---

## Icons

Place icon files (PNG/SVG) in `plugins/icons/` and reference them:
```python
icon_url="/api/plugin-icons/my_bank.png"
```

Icons are served automatically at `/api/plugin-icons/<filename>`.

---

## Tips

- **Deduplication**: Use `source_id` (a hash of unique fields) so re-importing the same file doesn't create duplicate entries.
- **Date format**: Always output dates as `YYYY-MM-DD`. Parse whatever the source provides internally.
- **Amount sign**: For banks/cards: negative = spent, positive = received. Be consistent.
- **Testing**: Run the app, open Imports, select your source, upload a file. Check the preview before committing.
- **Errors**: If your parser raises an exception, the import shows the error message to the user. Validate early.
- **Multiple sources per file**: One `register()` can return a list of `ParserPlugin` for different sources.
