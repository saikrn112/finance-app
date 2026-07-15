# Finance App

A self-hosted personal finance tracker with multi-currency support, statement import, Plaid bank sync, and an extensible plugin architecture.

## Features

- **Statement Import** — Parse bank/credit card PDF statements and CSV exports via plugins
- **Plaid Integration** — Live bank account sync with automatic transaction deduplication
- **Multi-Currency** — SQL-native currency conversion with live exchange rates
- **Recurring Detection** — Automatic subscription/recurring charge identification
- **Category Engine** — Rule-based transaction categorization with manual overrides
- **Payroll Tracking** — Parse payslip PDFs for gross/net/tax/deduction history
- **Retirement & Investments** — Track 401k contributions, HSA holdings, brokerage accounts
- **Net Worth** — Historical net worth chart with per-source breakdown
- **Google Drive Backup** — Encrypted vault backup/restore via Google Drive
- **Privacy Mode** — Mask all financial values with one toggle
- **Dark/Light Theme** — Complete theme token system
- **Demo Mode** — Fully functional demo with seed data (no credentials needed)
- **Plugin Architecture** — Add new banks/institutions by dropping a .py file

## Quick Start

Requires Docker Desktop or [Finch](https://github.com/runfinch/finch).

```bash
git clone <repository-url>
cd finance-app
bash scripts/start.sh
```

Open http://localhost:5173

The app boots with a fresh empty database. To try with demo data:

```bash
bash scripts/start.sh demo
# Open http://localhost:5174
```

### Using with real data

1. Copy `config.yaml.example` to `config.yaml`
2. Add your [Plaid API keys](https://dashboard.plaid.com/developers/keys) (free sandbox available)
3. Restart: `bash scripts/stop.sh && bash scripts/start.sh`
4. Connect your bank accounts via Plaid in Settings
5. Or import statement PDFs/CSVs via the Import panel

### Controls

```bash
bash scripts/start.sh        # Start prod
bash scripts/start.sh demo   # Start demo mode
bash scripts/stop.sh         # Stop
```

## Plugin System

Add support for your bank by creating a plugin file in any of these locations:

```
plugins/                     # Public plugin contract only; provider files are ignored
~/.finance-app/plugins/      # User-level (personal, gitignored)
$FINANCE_PLUGINS_DIR/        # Custom path via environment variable
```

Each plugin exports a `register()` function returning a `ParserPlugin`. Supports four record types:
- **transactions** — Bank statements, credit card statements, CSV exports
- **payslip** — Payroll/paycheck PDFs
- **retirement** — 401k, pension, provident fund CSVs/statements
- **investment** — Brokerage, HSA, trading account CSVs

See `plugins/HOW_TO_ADD_PLUGIN.md` for full documentation with examples.

Personal provider plugins, categorization rules, and runtime data should stay outside this repository. The personal plugin repository contains the privacy audit used before publishing.

## Architecture

```
src/
├── api/              # FastAPI routes
├── models/           # SQLAlchemy ORM (SQLite)
├── plugins/          # Plugin loader + registry
├── ingestion/        # Import pipeline + dedup
├── processing/       # Categorization, subscriptions
├── services/         # Exchange rates, currency
└── vault/            # Google Drive backup/restore

frontend/             # React + Vite + TailwindCSS
plugins/              # Plugin contract and local ignored extension point
data/                 # Runtime data (gitignored)
```

## Configuration

| Setting | Source | Notes |
|---------|--------|-------|
| Plaid keys | `config.yaml` | Get from [dashboard.plaid.com](https://dashboard.plaid.com) |
| Google Drive | `config.yaml.example` | Uses the app-owned desktop OAuth client |
| Display currency | In-app Settings | USD, INR, EUR supported |
| Category rules | `rules/categories.yaml` | YAML pattern matching |

## License

MIT
