# AGENTS.md

Orientation for AI agents working on this repo. Read this before touching code.

`README.md` covers what the app *does* from a user's point of view. This file covers
what you need to know to *change* it safely.

---

## 1. What this is

A self-hosted personal finance tracker. Runtime databases can contain real financial
history. Treat every non-demo database, backup, import, API response, and log as private
production data unless the user explicitly says otherwise.

Stack: FastAPI + SQLAlchemy + SQLite (backend), React + TypeScript + Vite + ag-grid
(frontend), Plaid for bank sync, PDF/CSV parsers for statements.

```
src/
  api/routes/        FastAPI routers, one per domain (transactions, projects, sync, ...)
  api/schemas.py     Pydantic response models  ← see caveat #2, this silently drops fields
  models/            SQLAlchemy models (transaction.py holds nearly all of them)
  ingestion/         Plaid sync, CSV/PDF import, backfill
  processing/        Categorizer, recurring detection, reconciliation
  plugins/           Plugin loader + registry (base.py defines ParserPlugin)
  services/          Exchange rates, auto-tasks, account values
  vault/             Google Drive backup/restore
frontend/src/
  Ledger.tsx         ag-grid transaction table — shared by main view AND project view
  ProjectsPage.tsx   Projects + members/splits UI
  SettingsPanel.tsx  Settings, Plaid accounts, vault, contacts manager
  api.ts             All API calls + TS types
scripts/             start/stop for both container and local modes
plugins/             In-repo plugins (bundled examples)
tests/               pytest regression and integration coverage
```

---

## 2. Running the app

Private/provider-specific plugins live **outside** the repo so personal parsers and rules
are never committed. Resolve them through `FINANCE_PLUGINS_DIR`. If the user supplied a
plugin repository/path earlier in the conversation, use it. Otherwise inspect the
configured environment; if it is unavailable, ask before creating or changing private
plugin code. Never guess or hardcode a personal filesystem path.

### Containerized (the owner's normal path)

```bash
cd /path/to/finance-app-prod

# start (auto-picks a free port pair, prompts if already running)
FINANCE_PLUGINS_DIR=/path/to/private-plugins bash scripts/start.sh --restart

# stop
bash scripts/stop.sh          # all modes
bash scripts/stop.sh prod     # just prod

# status
bash scripts/status.sh
```

Docker Desktop or Finch must be running; `start.sh` tries Docker then falls back to
Finch. Default ports are 8000 (API) and 5173 (web) — if taken it walks a candidate list
(8011/5181, 8021/5191, ...), so **read the script's output for the actual URL** instead
of assuming 5173.

Modes: `prod` (default), `demo` (seeded fake data, safe to experiment in), `replay`.

### Local, no container

```bash
bash scripts/install.sh                                   # once: .venv + npm install
FINANCE_PLUGINS_DIR=/path/to/private-plugins bash scripts/start_local.sh --restart
bash scripts/stop_local.sh
```

`start_local.sh` mirrors `start.sh` (same flags, same port scanning, same modes). It
waits for `/api/health` before starting Vite, and tracks PIDs in
`data/runtime/<mode>/.local_pids`.

### Which changes need a restart

| Changed | Action |
| --- | --- |
| `frontend/**` | Nothing — Vite HMR picks it up. Hard-refresh if state looks stale. |
| `src/**` (Python) | Restart the backend. |
| `$FINANCE_PLUGINS_DIR/rules/categories.yaml` | Restart (rules load at startup). |
| New plugin `.py` | Restart. |

Verify a frontend change actually shipped rather than assuming:
`curl -s http://localhost:5173/src/Ledger.tsx | grep <your-symbol>`

---

## 3. Hard rules

### Run the placement and privacy gate before editing

Before changing any file, state internally:

1. Is this generic infrastructure or provider/user-specific behavior?
2. Does the proposed code, fixture, comment, log, API response, or artifact contain data
   learned from a private database, statement, screenshot, plugin, or conversation?
3. If provider-specific, is the configured private plugin repository available or has
   the user already supplied its location?

If provider-specific, change the private plugin. If its location is unknown, ask the
user; do not put a fallback implementation in core while waiting. If private information
would enter the public tree or response surface, stop and warn the user. The safe default
is provider-neutral behavior plus an explicit unsupported/missing-plugin message, never a
hardcoded parser, alias, institution check, or copied production fixture.

### Never read money fields directly

Every monetary column is deliberately booby-trapped. `Transaction.amount`,
`AccountSnapshot.current_value`, `Payslip.gross`, `InvestmentHoldingSnapshot.value`,
`SourceBalanceHistory.value`, `AccountActivity.amount` and friends all raise
`AttributeError` on read.

Use the private `_`-prefixed column joined against a rate subquery, so conversion is
SQL-native and multi-currency stays correct:

```python
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh

ensure_rates_fresh(db)
lr = latest_rate_subquery(db)
total = (
    db.query(func.sum(Transaction._amount * lr.c.rate))
    .join(lr, and_(
        lr.c.from_currency == Transaction.currency,
        lr.c.to_currency == literal(currency),
    ))
    .scalar()
)
```

Writing is fine (`txn.amount = -12.34` works via the setter). It's *reading* that's
blocked. If you need a plain number for serialization, read `_amount` and multiply by a
rate from a rate map — see `_serialize_transaction` in `src/api/routes/projects.py`.

Endpoints that report money take a **required** `currency: str = Query(...)`. Don't
default it.

### Don't commit secrets or data

`config.yaml` (Plaid + Google client secrets) and `data/` are gitignored and must stay
that way. Only `config.yaml.example` is tracked. Before any commit, confirm neither
appears in `git status`.

### Keep personal and provider-specific details out of core

The public repository must not contain real names, addresses, email addresses, account
numbers, balances, transaction descriptions, employer data, access tokens, Plaid Item
IDs, vault/device IDs, absolute user paths, screenshots, statement excerpts, or values
copied from production. Tests and docs use obviously synthetic institutions and people.

Before finishing, inspect the complete diff for private literals and production payloads,
including logs/comments/test fixtures. Diagnostic APIs and UI must expose only the
minimum necessary information: prefer opaque/truncated hashes and safe labels over raw
paths or stable external identifiers. Never paste unredacted production rows into source,
artifacts, commits, or final responses.

Provider-specific parsing, aliases, categorization rules, institution behavior, labels,
and source normalization belong in the gitignored plugin repository, not `src/` or public
fixtures. Before implementing such behavior, check the plugin registry and configured
private plugin directory. If placement is uncertain, warn the user and default to a
generic, provider-neutral core behavior; do not add a convenient provider check to core.
Basic provider parsers under the bundled `plugins/` directory are intentional public
templates so a new user can get started. Keep them generic and free of personal data.
Provider-specific extensions, aliases, institution quirks, and owner-specific rules still
belong in the private plugin repository; do not expand a template to absorb those details.

### Persist financial history; do not fabricate it in analytics

The database is the source of truth. Imports, sync jobs, and explicit idempotent backfill
commands may normalize and persist historical facts. API/analytics routes should query
those stored facts and perform currency conversion or explicitly approved interpolation,
not reverse transactions, carry balances backward, synthesize snapshots, infer missing
provider history, or silently repair data at request time.

When history is missing or wrong:

1. Identify the authoritative provider/import/transaction inputs and the exact missing
   persisted rows.
2. Fix ingestion or the private provider plugin for future users.
3. Add an idempotent, reviewable CLI backfill for existing databases, with preview,
   provenance, row counts, and tests.
4. Run it only with user approval and a database backup.
5. Keep interpolation visibly separate from observed values; never cache fabricated
   interpolation as observed history.

Reject surface-level UI fallbacks and analytics-layer reconstructions that merely hide a
data defect. If a temporary fallback is unavoidable, warn the user, label it explicitly,
and add a tracked removal condition. Do not quietly turn it into permanent behavior.

Investment period inflow/gain is stored in `investment_period_facts`. Read routes must not
reconstruct it from account activity. Missing facts require an explicit ingestion/backfill
job with provenance, not an API fallback.

### Confirm before destructive DB work

Do not infer safety from a directory or environment name: staging/dev databases may be
restores of real data. Confirm the target database and mode. Backfills, bulk `UPDATE`s,
and `ALTER TABLE`s require a before/after row count or diff and a `SELECT` preview first.
Back up the exact target database to `/tmp/` for anything risky.

---

## 4. Caveats that have already bitten

1. **`init_db()` won't add columns to existing tables.** `Base.metadata.create_all` only
   creates *missing tables*. Adding a column to a live table needs an explicit
   `ALTER TABLE ... ADD COLUMN`, run against the real DB. Symptom: model has the field,
   queries fail or silently return nothing.

2. **Pydantic `response_model` silently strips unknown keys.** A route can build a dict
   containing your new field and the client still receives nothing, with no error. If you
   add a field to a response, add it to the matching model in `src/api/schemas.py` too
   (e.g. `ProjectDetailResponse.members`, `TransactionItem.splits`). Symptom: correct data
   in the DB, `null`/missing in the browser.

3. **Plugin paths must honour `FINANCE_PLUGINS_DIR`.** Anything that reads or writes
   `categories.yaml` must resolve it from that env var, falling back to the repo's
   `rules/categories.yaml`. Three separate call sites already needed fixing for this
   (`processing/categorizer.py`, and `_load_rule_categories` / `_append_rule_to_yaml` in
   `api/routes/transactions.py`). A hardcoded relative path *appears* to work because it
   falls back to the bundled defaults.

4. **The plugins volume must be writable.** Learned categorization rules are appended to
   `$FINANCE_PLUGINS_DIR/rules/categories.yaml` at runtime. Mounting it `:ro` surfaces as
   an opaque 500 from the uncategorized-review "Apply" button.

5. **ag-grid here is Community, not Enterprise.** No set/checkbox column filter —
   `filter: true` gives a *text* filter. Multi-select filtering needs a custom
   `headerComponent` (see `SplitFilterHeader` in `Ledger.tsx`). Also: ag-grid header cells
   swallow `onClick`, so use `onPointerDown` with `stopPropagation()` + `preventDefault()`,
   and render popups through `createPortal` to escape grid overflow clipping.

6. **`Ledger.tsx` is shared between the main ledger and the project view.** The
   `projectId` prop is the switch, and it decides more than visibility:
   - Project-only columns (Split, its header filter) must stay behind that check or they
     leak into the main ledger.
   - The note in the merchant cell targets different storage per view — the per-project
     `transaction_projects.description` vs. the transaction's own `notes`.
   - Several column widths shrink in project view. The grid sets
     `suppressHorizontalScroll`, so adding a column means shrinking others or the existing
     ones get crushed into `P...` / `R...` ellipses.
   - Project view uses `domLayout="autoHeight"` so the grid fits its rows exactly; the main
     view keeps a fixed viewport with internal scrolling because it can hold thousands of
     rows. Keep row height uniform — variable-height rows make any height math guesswork.

7. **Recharts `Pie` radii must be percentages, not pixels.** `innerRadius={72}` silently
   clips the donut into disconnected arcs once the container is narrower than
   `2 * outerRadius`. Use `innerRadius="55%" outerRadius="85%"` and put `min-w-0` on the
   chart's grid cell (`fr` columns don't shrink below content width without it).

8. **Don't size panes with `calc(100vh - Npx)`.** Two pages had dead space at the bottom
   from a magic number that no longer matched the real header height. Prefer
   `h-screen flex flex-col overflow-hidden` on the shell with `min-h-0 flex-1` on the
   scrolling pane, and cap any panel that can grow unboundedly (e.g. a staged-changes
   queue) with a `maxHeight` so it scrolls internally instead of squeezing its siblings.

9. **Vite's `loadEnv` doesn't read shell env vars.** `vite.config.ts` must consult
   `process.env.X` as well, otherwise the proxy target falls back to `localhost:8000` and
   every `/api` call fails with `ECONNREFUSED` on non-default ports.

10. **`kill 0` for child cleanup.** `npx` forks a grandchild, so killing the recorded PID
   leaves the real process alive. Call the binary directly (`node_modules/.bin/vite`) and
   clean up the whole process group.

11. **The 24h auto-task timer resets on restart.** `services/auto_tasks.py` sleeps
   `AUTO_SYNC_INTERVAL` before the first backup, so a frequently-restarted app never backs
   up automatically. The loop body is exception-guarded now — a crash inside it used to
   kill the thread silently for the rest of the process's life.

12. **Plaid can be 24–48h behind, and that is not necessarily a bug.** Missing recent transactions are
    often provider latency. Check `MAX(date)` per
    account and the Plaid dashboard's item log before touching sync code.

13. **Backfill is CLI-only, deliberately.** `python -m src.main backfill-schema` /
    `backfill-payslips`. It was removed from startup so it can't quietly rewrite rows on
    every boot.

---

## 5. Domain notes

**Categories** are a flat `"Top/Sub"` string on `Transaction.category`, not a table. The
dropdown is derived at runtime from the union of rules-yaml categories and distinct DB
values, so a category "exists" as soon as one row or rule uses it. Match existing
categories when writing rules — check
`SELECT DISTINCT category FROM transactions` before inventing new ones.

**Plugins** self-register via a `register()` returning `ParserPlugin` (see
`plugins/HOW_TO_ADD_PLUGIN.md`). `domain` is one of `financial_accounts`, `investments`,
`retirement`, `payroll`, and drives sidebar grouping. A private plugin with no parser is
a valid way to provide classification metadata without adding provider checks to core.

**Projects** cross-cut categories (a trip, a move). `transaction_projects` is the join
table and carries a per-project `description`. Split tracking uses a global `contacts`
list, `project_members` per project, and `transaction_project_splits` per transaction.
Adding a transaction to a project auto-assigns all current members. The split filter is
**exact match** — selected people must be exactly the transaction's split set.

**Privacy mask** (`FINANCE_APP_PRIVACY_MASK`) rewrites JSON response bodies to fake
values for screenshots. If numbers look wrong in a demo, check whether this is on.

---

## 6. Before you finish

```bash
cd frontend && npx tsc -p tsconfig.app.json --noEmit    # the only reliable form
python -m pytest tests/ -q                              # needs: pip install -e '.[test]'
```

**Two typecheck commands here lie to you. Use the one above.**

- `npx tsc --noEmit` — checks **nothing**. The root `tsconfig.json` is solution-style
  (`"files": []` plus references), so tsc exits 0 having examined zero files. This let a
  real `ReferenceError` (undefined name in a `useMemo` dep array) reach the browser under
  a "clean" typecheck.
- `npx tsc -b --noEmit` — incremental and unreliable here; reports 0 errors on repeat
  runs even when errors exist, because it treats cached projects as up to date.

Pointing at `tsconfig.app.json` directly avoids both traps and gives a stable count.

The frontend has **~59 pre-existing errors** (mostly `ImportWorkflow.tsx`, `PayslipPage.tsx`,
`CsvUpload.tsx`), so it is not clean repo-wide. Don't fix them as a side quest. Instead
record the count before and after your change and confirm it didn't grow:

```bash
npx tsc -p tsconfig.app.json --noEmit 2>&1 | grep -c 'error TS'
```

The suite has **~62 pre-existing failures**. Same rule: compare before/after rather than
expecting green. Attribute honestly — `git stash` also stashes the user's in-flight work,
so a naive stash-diff will blame their bugs on you. Check whether the failing file is one
you actually touched (`git diff --stat <file>`).

Commit only what you touched; `git status` should never show `config.yaml` or `data/`.

### Don't call it done because the API works

The expensive failures in this repo have all been **frontend wiring on top of a correct
backend**. A green `curl` proves nothing about whether the feature is usable. Before
claiming a UI change works:

1. **Add a backend test for the data flow.** `tests/test_api.py` has fixtures (`client`,
   `client_with_data`) that make this cheap — see `TestProjectsAndSplits`. Note
   `client` yields `(client, Session)` but `client_with_data` yields the client alone.
2. **Then actually drive the UI**, or say plainly that you could not. A manual API
   round-trip is a *backend* check; label it as one. Don't let "I verified it" mean
   "I verified the half I could reach."
3. **Reload fully after refactors that change hook order or move JSX between
   components.** Vite Fast Refresh cannot always patch these, and a stale module reads
   exactly like a broken feature.
4. **Watch for these React/ag-grid traps** — each one shipped a "working" feature that
   didn't work:
   - Putting form state in a dependency array of a `useCallback` cell renderer. Every
     keystroke changes the renderer's identity → `colDefs` changes → ag-grid rebuilds
     columns → the input remounts and **loses focus after one character**. Render popups
     and forms from the component body, not from inside a cell.
   - Rendering a popup inside a cell renderer at all: row virtualization unmounts it the
     moment the row scrolls out of view.
   - Scroll chaining. A scrollable popup list that hits its end passes the scroll to the
     grid behind it. Add `overscroll-contain`.
   - Popups anchored with `position: fixed` need a height cap plus an upward flip when the
     trigger sits low, or the content runs off-screen with no way to reach it.

If you cannot verify something (no browser access, missing dev dependency, provider
latency), say so explicitly in your final message and name what remains unverified.
Silence reads as "tested".
