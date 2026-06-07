from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.transaction import Project, SyncLog, Transaction, TransactionProject


DEMO_TRANSACTIONS = [
    {"source_id": "demo-bank-pay-2025-10-15", "source": "First National Bank", "date": date(2025, 10, 15), "amount": Decimal("5000.00"), "merchant_raw": "ACME CORP PAYROLL", "merchant_clean": "Acme Corp Payroll", "category": "Salary/Paycheck", "account_last4": "4321"},
    {"source_id": "demo-bank-rent-2025-11-01", "source": "First National Bank", "date": date(2025, 11, 1), "amount": Decimal("-1800.00"), "merchant_raw": "OAK RIDGE APARTMENTS", "merchant_clean": "Oak Ridge Apartments", "category": "Rent", "account_last4": "4321"},
    {"source_id": "demo-bank-electric-2025-11-04", "source": "First National Bank", "date": date(2025, 11, 4), "amount": Decimal("-96.42"), "merchant_raw": "CITY ELECTRIC", "merchant_clean": "City Electric", "category": "Utilities/Electric", "account_last4": "4321"},
    {"source_id": "demo-bank-grocery-2025-11-08", "source": "First National Bank", "date": date(2025, 11, 8), "amount": Decimal("-118.37"), "merchant_raw": "TRADER JOES", "merchant_clean": "Trader Joe's", "category": "Groceries", "account_last4": "4321"},
    {"source_id": "demo-bank-pay-2025-11-15", "source": "First National Bank", "date": date(2025, 11, 15), "amount": Decimal("5000.00"), "merchant_raw": "ACME CORP PAYROLL", "merchant_clean": "Acme Corp Payroll", "category": "Salary/Paycheck", "account_last4": "4321"},
    {"source_id": "demo-bank-rent-2025-12-01", "source": "First National Bank", "date": date(2025, 12, 1), "amount": Decimal("-1800.00"), "merchant_raw": "OAK RIDGE APARTMENTS", "merchant_clean": "Oak Ridge Apartments", "category": "Rent", "account_last4": "4321"},
    {"source_id": "demo-bank-pay-2025-12-15", "source": "First National Bank", "date": date(2025, 12, 15), "amount": Decimal("5000.00"), "merchant_raw": "ACME CORP PAYROLL", "merchant_clean": "Acme Corp Payroll", "category": "Salary/Paycheck", "account_last4": "4321"},
    {"source_id": "demo-bank-uhaul-2026-01-03", "source": "First National Bank", "date": date(2026, 1, 3), "amount": Decimal("-229.14"), "merchant_raw": "U-HAUL", "merchant_clean": "U-Haul", "category": "Transportation/Car", "account_last4": "4321"},
    {"source_id": "demo-bank-rent-2026-01-01", "source": "First National Bank", "date": date(2026, 1, 1), "amount": Decimal("-1800.00"), "merchant_raw": "OAK RIDGE APARTMENTS", "merchant_clean": "Oak Ridge Apartments", "category": "Rent", "account_last4": "4321"},
    {"source_id": "demo-bank-ikea-2026-01-12", "source": "First National Bank", "date": date(2026, 1, 12), "amount": Decimal("-624.88"), "merchant_raw": "IKEA", "merchant_clean": "IKEA", "category": "Shopping/Home", "account_last4": "4321"},
    {"source_id": "demo-bank-taskrabbit-2026-01-14", "source": "First National Bank", "date": date(2026, 1, 14), "amount": Decimal("-180.00"), "merchant_raw": "TASKRABBIT", "merchant_clean": "TaskRabbit", "category": "Shopping/Home", "account_last4": "4321"},
    {"source_id": "demo-bank-pay-2026-01-15", "source": "First National Bank", "date": date(2026, 1, 15), "amount": Decimal("5000.00"), "merchant_raw": "ACME CORP PAYROLL", "merchant_clean": "Acme Corp Payroll", "category": "Salary/Paycheck", "account_last4": "4321"},
    {"source_id": "demo-bank-rent-2026-02-01", "source": "First National Bank", "date": date(2026, 2, 1), "amount": Decimal("-1800.00"), "merchant_raw": "OAK RIDGE APARTMENTS", "merchant_clean": "Oak Ridge Apartments", "category": "Rent", "account_last4": "4321"},
    {"source_id": "demo-bank-safeway-2026-02-06", "source": "First National Bank", "date": date(2026, 2, 6), "amount": Decimal("-132.48"), "merchant_raw": "SAFEWAY", "merchant_clean": "Safeway", "category": "Groceries", "account_last4": "4321"},
    {"source_id": "demo-bank-pay-2026-02-14", "source": "First National Bank", "date": date(2026, 2, 14), "amount": Decimal("5000.00"), "merchant_raw": "ACME CORP PAYROLL", "merchant_clean": "Acme Corp Payroll", "category": "Salary/Paycheck", "account_last4": "4321"},
    {"source_id": "demo-bank-rent-2026-03-01", "source": "First National Bank", "date": date(2026, 3, 1), "amount": Decimal("-1800.00"), "merchant_raw": "OAK RIDGE APARTMENTS", "merchant_clean": "Oak Ridge Apartments", "category": "Rent", "account_last4": "4321"},
    {"source_id": "demo-bank-electric-2026-03-02", "source": "First National Bank", "date": date(2026, 3, 2), "amount": Decimal("-92.15"), "merchant_raw": "CITY ELECTRIC", "merchant_clean": "City Electric", "category": "Utilities/Electric", "account_last4": "4321"},

    {"source_id": "demo-premium-gas-2025-11-03", "source": "Premium Card", "date": date(2025, 11, 3), "amount": Decimal("-64.21"), "merchant_raw": "COSTCO GAS", "merchant_clean": "Costco Gas", "category": "Transportation/Gas", "account_last4": "9001"},
    {"source_id": "demo-premium-chipotle-2025-11-06", "source": "Premium Card", "date": date(2025, 11, 6), "amount": Decimal("-18.42"), "merchant_raw": "CHIPOTLE", "merchant_clean": "Chipotle", "category": "Dining", "account_last4": "9001"},
    {"source_id": "demo-premium-gas-2025-12-05", "source": "Premium Card", "date": date(2025, 12, 5), "amount": Decimal("-58.94"), "merchant_raw": "COSTCO GAS", "merchant_clean": "Costco Gas", "category": "Transportation/Gas", "account_last4": "9001"},
    {"source_id": "demo-premium-openai-2025-12-12", "source": "Premium Card", "date": date(2025, 12, 12), "amount": Decimal("-20.00"), "merchant_raw": "OPENAI", "merchant_clean": "OpenAI", "category": "Subscriptions/Tech", "account_last4": "9001"},
    {"source_id": "demo-premium-jrpass-2025-12-28", "source": "Premium Card", "date": date(2025, 12, 28), "amount": Decimal("-45.00"), "merchant_raw": "SUICA RELOAD", "merchant_clean": "Suica Reload", "category": "Transportation/Transit", "account_last4": "9001"},
    {"source_id": "demo-premium-ramen-2025-12-29", "source": "Premium Card", "date": date(2025, 12, 29), "amount": Decimal("-36.50"), "merchant_raw": "ICHIRAN", "merchant_clean": "Ichiran", "category": "Dining", "account_last4": "9001"},
    {"source_id": "demo-premium-gas-2026-01-05", "source": "Premium Card", "date": date(2026, 1, 5), "amount": Decimal("-61.14"), "merchant_raw": "COSTCO GAS", "merchant_clean": "Costco Gas", "category": "Transportation/Gas", "account_last4": "9001"},
    {"source_id": "demo-premium-openai-2026-01-12", "source": "Premium Card", "date": date(2026, 1, 12), "amount": Decimal("-20.00"), "merchant_raw": "OPENAI", "merchant_clean": "OpenAI", "category": "Subscriptions/Tech", "account_last4": "9001"},
    {"source_id": "demo-premium-homedepot-2026-01-18", "source": "Premium Card", "date": date(2026, 1, 18), "amount": Decimal("-340.22"), "merchant_raw": "HOME DEPOT", "merchant_clean": "Home Depot", "category": "Shopping/Home", "account_last4": "9001"},
    {"source_id": "demo-premium-southwest-2026-02-21", "source": "Premium Card", "date": date(2026, 2, 21), "amount": Decimal("-420.00"), "merchant_raw": "SOUTHWEST AIRLINES", "merchant_clean": "Southwest Airlines", "category": "Travel/Flights", "account_last4": "9001"},
    {"source_id": "demo-premium-gas-2026-03-05", "source": "Premium Card", "date": date(2026, 3, 5), "amount": Decimal("-63.11"), "merchant_raw": "COSTCO GAS", "merchant_clean": "Costco Gas", "category": "Transportation/Gas", "account_last4": "9001"},

    {"source_id": "demo-rewards-netflix-2025-11-10", "source": "Rewards Card", "date": date(2025, 11, 10), "amount": Decimal("-15.49"), "merchant_raw": "NETFLIX", "merchant_clean": "Netflix", "category": "Subscriptions/Entertainment", "account_last4": "2100"},
    {"source_id": "demo-rewards-headspace-2025-10-08", "source": "Rewards Card", "date": date(2025, 10, 8), "amount": Decimal("-12.99"), "merchant_raw": "HEADSPACE", "merchant_clean": "Headspace", "category": "Subscriptions/Health", "account_last4": "2100"},
    {"source_id": "demo-rewards-headspace-2025-11-08", "source": "Rewards Card", "date": date(2025, 11, 8), "amount": Decimal("-12.99"), "merchant_raw": "HEADSPACE", "merchant_clean": "Headspace", "category": "Subscriptions/Health", "account_last4": "2100"},
    {"source_id": "demo-rewards-headspace-2025-12-08", "source": "Rewards Card", "date": date(2025, 12, 8), "amount": Decimal("-12.99"), "merchant_raw": "HEADSPACE", "merchant_clean": "Headspace", "category": "Subscriptions/Health", "account_last4": "2100"},
    {"source_id": "demo-rewards-netflix-2025-12-10", "source": "Rewards Card", "date": date(2025, 12, 10), "amount": Decimal("-15.49"), "merchant_raw": "NETFLIX", "merchant_clean": "Netflix", "category": "Subscriptions/Entertainment", "account_last4": "2100"},
    {"source_id": "demo-rewards-ana-2025-12-22", "source": "Rewards Card", "date": date(2025, 12, 22), "amount": Decimal("-890.00"), "merchant_raw": "ANA AIR", "merchant_clean": "ANA", "category": "Travel/Flights", "account_last4": "2100"},
    {"source_id": "demo-rewards-hotel-2025-12-27", "source": "Rewards Card", "date": date(2025, 12, 27), "amount": Decimal("-520.00"), "merchant_raw": "HOTEL TOKYO", "merchant_clean": "Hotel Tokyo", "category": "Travel/Hotel", "account_last4": "2100"},
    {"source_id": "demo-rewards-netflix-2026-01-10", "source": "Rewards Card", "date": date(2026, 1, 10), "amount": Decimal("-15.49"), "merchant_raw": "NETFLIX", "merchant_clean": "Netflix", "category": "Subscriptions/Entertainment", "account_last4": "2100"},
    {"source_id": "demo-rewards-disney-2026-01-20", "source": "Rewards Card", "date": date(2026, 1, 20), "amount": Decimal("-13.99"), "merchant_raw": "DISNEY+", "merchant_clean": "Disney+", "category": "Subscriptions/Entertainment", "account_last4": "2100"},
    {"source_id": "demo-rewards-sweetgreen-2026-02-11", "source": "Rewards Card", "date": date(2026, 2, 11), "amount": Decimal("-19.24"), "merchant_raw": "SWEETGREEN", "merchant_clean": "Sweetgreen", "category": "Dining", "account_last4": "2100"},
    {"source_id": "demo-rewards-netflix-2026-02-10", "source": "Rewards Card", "date": date(2026, 2, 10), "amount": Decimal("-15.49"), "merchant_raw": "NETFLIX", "merchant_clean": "Netflix", "category": "Subscriptions/Entertainment", "account_last4": "2100"},
    {"source_id": "demo-rewards-disney-2026-02-20", "source": "Rewards Card", "date": date(2026, 2, 20), "amount": Decimal("-13.99"), "merchant_raw": "DISNEY+", "merchant_clean": "Disney+", "category": "Subscriptions/Entertainment", "account_last4": "2100"},

    {"source_id": "demo-rewards-spotify-2025-11-09", "source": "Rewards Card", "date": date(2025, 11, 9), "amount": Decimal("-10.99"), "merchant_raw": "SPOTIFY", "merchant_clean": "Spotify", "category": "Subscriptions/Entertainment", "account_last4": "5544"},
    {"source_id": "demo-rewards-spotify-2025-12-09", "source": "Rewards Card", "date": date(2025, 12, 9), "amount": Decimal("-10.99"), "merchant_raw": "SPOTIFY", "merchant_clean": "Spotify", "category": "Subscriptions/Entertainment", "account_last4": "5544"},
    {"source_id": "demo-rewards-spotify-2026-01-09", "source": "Rewards Card", "date": date(2026, 1, 9), "amount": Decimal("-11.99"), "merchant_raw": "SPOTIFY", "merchant_clean": "Spotify", "category": "Subscriptions/Entertainment", "account_last4": "5544"},
    {"source_id": "demo-rewards-spotify-2026-02-09", "source": "Rewards Card", "date": date(2026, 2, 9), "amount": Decimal("-11.99"), "merchant_raw": "SPOTIFY", "merchant_clean": "Spotify", "category": "Subscriptions/Entertainment", "account_last4": "5544"},
    {"source_id": "demo-rewards-wholefoods-2026-02-25", "source": "Rewards Card", "date": date(2026, 2, 25), "amount": Decimal("-86.72"), "merchant_raw": "WHOLE FOODS", "merchant_clean": "Whole Foods", "category": "Groceries", "account_last4": "5544"},
    {"source_id": "demo-rewards-shell-2026-03-04", "source": "Rewards Card", "date": date(2026, 3, 4), "amount": Decimal("-52.10"), "merchant_raw": "SHELL", "merchant_clean": "Shell", "category": "Transportation/Gas", "account_last4": "5544"},

    {"source_id": "demo-cashback-icloud-2025-11-18", "source": "Cash Back Card", "date": date(2025, 11, 18), "amount": Decimal("-2.99"), "merchant_raw": "ICLOUD+", "merchant_clean": "iCloud+", "category": "Subscriptions/Tech", "account_last4": "7788"},
    {"source_id": "demo-cashback-icloud-2025-12-18", "source": "Cash Back Card", "date": date(2025, 12, 18), "amount": Decimal("-2.99"), "merchant_raw": "ICLOUD+", "merchant_clean": "iCloud+", "category": "Subscriptions/Tech", "account_last4": "7788"},
    {"source_id": "demo-cashback-icloud-2026-01-18", "source": "Cash Back Card", "date": date(2026, 1, 18), "amount": Decimal("-2.99"), "merchant_raw": "ICLOUD+", "merchant_clean": "iCloud+", "category": "Subscriptions/Tech", "account_last4": "7788"},
    {"source_id": "demo-cashback-icloud-2026-02-18", "source": "Cash Back Card", "date": date(2026, 2, 18), "amount": Decimal("-2.99"), "merchant_raw": "ICLOUD+", "merchant_clean": "iCloud+", "category": "Subscriptions/Tech", "account_last4": "7788"},
    {"source_id": "demo-cashback-peets-2026-02-26", "source": "Cash Back Card", "date": date(2026, 2, 26), "amount": Decimal("-8.12"), "merchant_raw": "PEETS COFFEE", "merchant_clean": "Peet's Coffee", "category": "Dining", "account_last4": "7788"},
    {"source_id": "demo-cashback-store-2026-03-03", "source": "Cash Back Card", "date": date(2026, 3, 3), "amount": Decimal("-249.00"), "merchant_raw": "APPLE STORE", "merchant_clean": "Apple Store", "category": "Shopping/Tech", "account_last4": "7788"},
    {"source_id": "demo-premium-disney-2025-11-20", "source": "Premium Card", "date": date(2025, 11, 20), "amount": Decimal("-13.99"), "merchant_raw": "DISNEY+", "merchant_clean": "Disney+", "category": "Subscriptions/Entertainment", "account_last4": "9001"},
    {"source_id": "demo-premium-disney-2025-12-20", "source": "Premium Card", "date": date(2025, 12, 20), "amount": Decimal("-13.99"), "merchant_raw": "DISNEY+", "merchant_clean": "Disney+", "category": "Subscriptions/Entertainment", "account_last4": "9001"},
    {"source_id": "demo-manual-ynab-2025-12-01", "source": "Manual", "date": date(2025, 12, 1), "amount": Decimal("-14.99"), "merchant_raw": "YNAB", "merchant_clean": "YNAB", "category": "Subscriptions/Tech", "account_last4": None},
    {"source_id": "demo-manual-ynab-2026-01-01", "source": "Manual", "date": date(2026, 1, 1), "amount": Decimal("-14.99"), "merchant_raw": "YNAB", "merchant_clean": "YNAB", "category": "Subscriptions/Tech", "account_last4": None},
    {"source_id": "demo-manual-ynab-2026-02-01", "source": "Manual", "date": date(2026, 2, 1), "amount": Decimal("-14.99"), "merchant_raw": "YNAB", "merchant_clean": "YNAB", "category": "Subscriptions/Tech", "account_last4": None},
    {"source_id": "demo-manual-ynab-2026-03-01", "source": "Manual", "date": date(2026, 3, 1), "amount": Decimal("-14.99"), "merchant_raw": "YNAB", "merchant_clean": "YNAB", "category": "Subscriptions/Tech", "account_last4": None},
    {"source_id": "demo-manual-obsidian-2025-03-07", "source": "Manual", "date": date(2025, 3, 7), "amount": Decimal("-96.00"), "merchant_raw": "OBSIDIAN", "merchant_clean": "Obsidian", "category": "Subscriptions/Tech", "account_last4": None},
    {"source_id": "demo-manual-obsidian-2026-03-07", "source": "Manual", "date": date(2026, 3, 7), "amount": Decimal("-96.00"), "merchant_raw": "OBSIDIAN", "merchant_clean": "Obsidian", "category": "Subscriptions/Tech", "account_last4": None},
]

DEMO_PROJECTS = [
    {"name": "Home Renovation", "color": "#22c55e", "budget": Decimal("4500.00"), "status": "active", "start_date": date(2026, 1, 1), "notes": "Furniture, movers, setup costs for the renovation."},
    {"name": "Japan Trip 2025", "color": "#3b82f6", "budget": Decimal("2800.00"), "status": "completed", "start_date": date(2025, 12, 20), "end_date": date(2025, 12, 30), "notes": "Flights, hotel, transit, and food from the year-end trip."},
]

DEMO_PROJECT_LINKS = {
    "Home Renovation": [
        "demo-bank-uhaul-2026-01-03",
        "demo-bank-ikea-2026-01-12",
        "demo-bank-taskrabbit-2026-01-14",
        "demo-premium-homedepot-2026-01-18",
    ],
    "Japan Trip 2025": [
        "demo-rewards-ana-2025-12-22",
        "demo-rewards-hotel-2025-12-27",
        "demo-premium-jrpass-2025-12-28",
        "demo-premium-ramen-2025-12-29",
    ],
}

DEMO_SYNC_LOGS = [
    {"institution_name": "First National Bank", "token": "demo-bank-token", "item_id": "demo-item-bank", "record_count": 17},
    {"institution_name": "Premium Card", "token": "demo-premium-token", "item_id": "demo-item-premium", "record_count": 11},
    {"institution_name": "Rewards Card", "token": "demo-rewards-token", "item_id": "demo-item-rewards", "record_count": 7},
    {"institution_name": "InvestCo", "token": "demo-investco-token", "item_id": "demo-item-investco", "record_count": 0},
    {"institution_name": "High Yield Savings", "token": "demo-hys-token", "item_id": "demo-item-hys", "record_count": 0},
]

DEMO_PLAID_BALANCES = [
    {"source": "First National Bank", "balance": 14682.47},
    {"source": "Premium Card", "balance": 1149.53},
    {"source": "Rewards Card", "balance": 1475.70},
    {"source": "High Yield Savings", "balance": 12840.32},
]

DEMO_INVESTMENTS = {
    "accounts": [
        {"account_id": "demo-investco-brokerage", "name": "InvestCo Brokerage", "type": "brokerage", "subtype": "individual"},
    ],
    "holdings": [
        {"ticker": "VTI", "name": "Vanguard Total Stock Market ETF", "quantity": 42.37, "price": 276.12, "value": 11698.60, "cost_basis": 10320.18, "type": "etf", "source": "InvestCo"},
        {"ticker": "AAPL", "name": "Apple Inc.", "quantity": 18.0, "price": 185.40, "value": 3337.20, "cost_basis": 2798.55, "type": "equity", "source": "InvestCo"},
        {"ticker": "NVDA", "name": "NVIDIA Corp.", "quantity": 12.0, "price": 118.50, "value": 1422.00, "cost_basis": 902.40, "type": "equity", "source": "InvestCo"},
        {"ticker": "MSFT", "name": "Microsoft Corp.", "quantity": 4.5, "price": 410.00, "value": 1845.00, "cost_basis": 1494.00, "type": "equity", "source": "InvestCo"},
        {"ticker": "CUR:USD", "name": "Cash", "quantity": 1.0, "price": 1375.44, "value": 1375.44, "cost_basis": 1375.44, "type": "cash", "source": "InvestCo"},
    ],
}

DEMO_RETIREMENT = {
    "transactions": [
        {"type": "Employee Pre-Tax", "date": "2025-12-15", "source": "Employee", "fund": "Vanguard 2060", "units": 8.4421, "unit_price": 128.11, "amount": 1081.82},
        {"type": "Employer Match", "date": "2025-12-15", "source": "Employer Match", "fund": "Vanguard 2060", "units": 4.2210, "unit_price": 128.11, "amount": 540.91},
        {"type": "Employee Pre-Tax", "date": "2026-01-15", "source": "Employee", "fund": "Vanguard 2060", "units": 8.1281, "unit_price": 132.85, "amount": 1079.20},
        {"type": "Employer Match", "date": "2026-01-15", "source": "Employer Match", "fund": "Vanguard 2060", "units": 4.0640, "unit_price": 132.85, "amount": 539.60},
        {"type": "Employee Pre-Tax", "date": "2026-02-14", "source": "Employee", "fund": "Vanguard 2060", "units": 7.8142, "unit_price": 138.22, "amount": 1080.54},
        {"type": "Employer Match", "date": "2026-02-14", "source": "Employer Match", "fund": "Vanguard 2060", "units": 3.9071, "unit_price": 138.22, "amount": 540.27},
    ],
    "summary": {
        "balance": 48210.64,
        "total_units": 348.8142,
        "unit_price": 138.22,
        "total_contributed": 18642.17,
        "employee_contributed": 12428.11,
        "employer_match": 6214.06,
        "fees": -32.18,
        "gain": 29600.65,
        "fund_name": "Vanguard 2060",
        "date_range": ["2025-01-15", "2026-02-14"],
    },
}

DEMO_PAYSLIPS = [
    {
        "employer": "Acme Corp",
        "pay_date": "12/15/2025",
        "period_start": "12/01/2025",
        "period_end": "12/15/2025",
        "gross": 6250.00,
        "net": 4012.50,
        "total_taxes": 1612.50,
        "total_deductions": 625.00,
        "taxes": {"federal": 820.00, "social_security": 387.50, "medicare": 90.63, "state_tax": 245.00, "state_disability": 69.37},
        "deductions": {"medical": 156.00, "dental": 18.00, "vision": 6.50, "hsa": 210.00, "401k": 234.50},
        "earnings": {"regular": 6250.00},
        "benefits": {},
        "gross_ytd": 75000.00,
        "hours": 80.0,
    },
    {
        "employer": "Acme Corp",
        "pay_date": "01/15/2026",
        "period_start": "01/01/2026",
        "period_end": "01/15/2026",
        "gross": 6250.00,
        "net": 4012.50,
        "total_taxes": 1612.50,
        "total_deductions": 625.00,
        "taxes": {"federal": 820.00, "social_security": 387.50, "medicare": 90.63, "state_tax": 245.00, "state_disability": 69.37},
        "deductions": {"medical": 156.00, "dental": 18.00, "vision": 6.50, "hsa": 210.00, "401k": 234.50},
        "earnings": {"regular": 6250.00},
        "benefits": {},
        "gross_ytd": 6250.00,
        "hours": 80.0,
    },
    {
        "employer": "Acme Corp",
        "pay_date": "02/14/2026",
        "period_start": "02/01/2026",
        "period_end": "02/14/2026",
        "gross": 6250.00,
        "net": 4012.50,
        "total_taxes": 1612.50,
        "total_deductions": 625.00,
        "taxes": {"federal": 820.00, "social_security": 387.50, "medicare": 90.63, "state_tax": 245.00, "state_disability": 69.37},
        "deductions": {"medical": 156.00, "dental": 18.00, "vision": 6.50, "hsa": 210.00, "401k": 234.50},
        "earnings": {"regular": 6250.00},
        "benefits": {},
        "gross_ytd": 12500.00,
        "hours": 80.0,
    },
]


def seed_demo_data(db: Session) -> None:
    existing_txns = {txn.source_id: txn for txn in db.query(Transaction).all()}
    transactions = []
    for item in DEMO_TRANSACTIONS:
        if item["source_id"] in existing_txns:
            txn = existing_txns[item["source_id"]]
            txn.source = item["source"]
            txn.date = item["date"]
            txn.amount = item["amount"]
            txn.merchant_raw = item["merchant_raw"]
            txn.merchant_clean = item["merchant_clean"]
            txn.category = item["category"]
            txn.category_source = "demo"
            txn.account_last4 = item["account_last4"]
            txn.origin = "demo"
            transactions.append(txn)
            continue
        txn = Transaction(
            source_id=item["source_id"],
            source=item["source"],
            date=item["date"],
            amount=item["amount"],
            merchant_raw=item["merchant_raw"],
            merchant_clean=item["merchant_clean"],
            category=item["category"],
            category_source="demo",
            account_last4=item["account_last4"],
            origin="demo",
        )
        transactions.append(txn)
        db.add(txn)
        existing_txns[item["source_id"]] = txn

    existing_project_names = {row[0] for row in db.query(Project.name).all()}
    projects = []
    for item in DEMO_PROJECTS:
        if item["name"] in existing_project_names:
            projects.append(db.query(Project).filter(Project.name == item["name"]).first())
            continue
        project = Project(
            name=item["name"],
            color=item["color"],
            budget=item.get("budget"),
            status=item["status"],
            start_date=item.get("start_date"),
            end_date=item.get("end_date"),
            notes=item.get("notes"),
        )
        projects.append(project)
        db.add(project)
        existing_project_names.add(item["name"])

    existing_item_ids = {
        row[0]
        for row in db.query(SyncLog.plaid_item_id)
        .filter(SyncLog.sync_type == "plaid", SyncLog.status == "connected")
        .all()
    }
    for item in DEMO_SYNC_LOGS:
        if item["item_id"] in existing_item_ids:
            continue
        db.add(
            SyncLog(
                source="plaid",
                sync_type="plaid",
                status="connected",
                plaid_item_id=item["item_id"],
                record_count=item["record_count"],
                extra_data={"access_token": item["token"], "institution_name": item["institution_name"], "demo": True},
            )
        )
        existing_item_ids.add(item["item_id"])

    db.flush()

    txns_by_source_id = {txn.source_id: txn for txn in transactions}
    projects_by_name = {project.name: project for project in projects}
    existing_project_links = {
        (row[0], row[1])
        for row in db.query(TransactionProject.transaction_id, TransactionProject.project_id).all()
    }
    for project_name, source_ids in DEMO_PROJECT_LINKS.items():
        project = projects_by_name[project_name]
        for source_id in source_ids:
            txn = txns_by_source_id[source_id]
            link_key = (txn.id, project.id)
            if link_key in existing_project_links:
                continue
            db.add(TransactionProject(transaction_id=txn.id, project_id=project.id))
            existing_project_links.add(link_key)

    db.commit()


def get_demo_investments() -> dict:
    return deepcopy(DEMO_INVESTMENTS)


def get_demo_plaid_balances() -> list[dict]:
    return deepcopy(DEMO_PLAID_BALANCES)


def get_demo_retirement() -> dict:
    return deepcopy(DEMO_RETIREMENT)


def get_demo_payslips() -> list[dict]:
    return deepcopy(DEMO_PAYSLIPS)


def get_demo_sidebar_accounts() -> list[dict]:
    totals: dict[str, float] = {}
    for txn in DEMO_TRANSACTIONS:
        totals[txn["source"]] = totals.get(txn["source"], 0.0) + float(txn["amount"])

    investco_value = sum(float(h["value"]) for h in DEMO_INVESTMENTS["holdings"] if h["source"] == "InvestCo")
    plaid_snapshot_map = {item["source"]: float(item["balance"]) for item in DEMO_PLAID_BALANCES}

    return [
        {
            "source": "First National Bank",
            "group": "bank_account",
            "connection_state": "plaid",
            "balance": plaid_snapshot_map.get("First National Bank"),
            "ledger_balance": round(totals.get("First National Bank", 0.0), 2),
            "snapshot_balance": plaid_snapshot_map.get("First National Bank"),
            "last_synced": None,
            "filter_source": "First National Bank",
        },
        {
            "source": "Premium Card",
            "group": "credit_card",
            "connection_state": "plaid",
            "balance": plaid_snapshot_map.get("Premium Card"),
            "ledger_balance": round(totals.get("Premium Card", 0.0), 2),
            "snapshot_balance": plaid_snapshot_map.get("Premium Card"),
            "last_synced": None,
            "filter_source": "Premium Card",
        },
        {
            "source": "Rewards Card",
            "group": "credit_card",
            "connection_state": "plaid",
            "balance": plaid_snapshot_map.get("Rewards Card"),
            "ledger_balance": round(totals.get("Rewards Card", 0.0), 2),
            "snapshot_balance": plaid_snapshot_map.get("Rewards Card"),
            "last_synced": None,
            "filter_source": "Rewards Card",
        },
        {
            "source": "Rewards Card",
            "group": "credit_card",
            "connection_state": "manual",
            "balance": round(totals.get("Rewards Card", 0.0), 2),
            "ledger_balance": round(totals.get("Rewards Card", 0.0), 2),
            "snapshot_balance": None,
            "last_synced": None,
            "filter_source": "Rewards Card",
        },
        {
            "source": "Cash Back Card",
            "group": "credit_card",
            "connection_state": "manual",
            "balance": round(totals.get("Cash Back Card", 0.0), 2),
            "ledger_balance": round(totals.get("Cash Back Card", 0.0), 2),
            "snapshot_balance": None,
            "last_synced": None,
            "filter_source": "Cash Back Card",
        },
        {
            "source": "High Yield Savings",
            "group": "investment",
            "connection_state": "plaid",
            "balance": plaid_snapshot_map.get("High Yield Savings"),
            "ledger_balance": None,
            "snapshot_balance": plaid_snapshot_map.get("High Yield Savings"),
            "last_synced": None,
            "filter_source": None,
        },
        {
            "source": "InvestCo",
            "group": "investment",
            "connection_state": "plaid",
            "balance": round(investco_value, 2),
            "ledger_balance": None,
            "snapshot_balance": round(investco_value, 2),
            "last_synced": None,
            "filter_source": None,
        },
        {
            "source": "National Retirement Plan",
            "group": "retirement",
            "connection_state": "manual",
            "balance": float(DEMO_RETIREMENT["summary"]["balance"]),
            "ledger_balance": None,
            "snapshot_balance": float(DEMO_RETIREMENT["summary"]["balance"]),
            "last_synced": None,
            "filter_source": None,
        },
    ]
