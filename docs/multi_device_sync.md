# Multi-device sync

Design of record for running the app on several devices at once — the container app, the macOS
desktop app, and a future iOS app — with all of them able to write.

Status: **in progress.** Phases A–E are implemented: identity, tombstones, the merge engine,
transports and the Plaid lease. **Nothing calls sync from the app yet** — no scheduler hook, no UI, no
device list — so merging this changes no runtime behaviour. `DriveTransport` has never been run
against real Google Drive with a real payload; the automated tests drive a fake transport, so
`DriveTransport` has no automated coverage at all. The `.fvault` vault backup is
untouched and remains the only cloud path in use.

---

## 1. Why the vault cannot do this

The vault is a **backup** system: each backup is a full `.fvault` archive of the whole database, and
one file per vault — `<prefix>-<vault_id>-latest.manifest.json` — is the head pointer.

Two consequences, both fatal for multi-device:

1. **Two devices write the same object.** `backup_vault_to_google_drive` reads the remote `latest`
   manifest only to get its Drive file id, then overwrites it. It never compares the remote head
   against this device's `parent_backup_id`. So if two devices back up, the second silently orphans
   the first's head; the archive survives in its own folder, but the lineage and the pointer are
   wrong and nobody is told. This is a lost update on the head pointer.
2. **Restore replaces the entire database.** There is no row identity and no change tracking, so two
   devices' concurrent edits cannot be reconciled at all — whoever restores last wins everything.

No amount of repair makes whole-database archives merge. Multi-device needs a different unit of
exchange, which is what this document describes. The vault stays as disaster recovery, which is a
different job and a good one.

## 2. The model, and what it is borrowed from

Timeslice solves this and is worth copying closely, because its core decision removes the problem
rather than managing it: **each device owns exactly one file, named from its own device id, so no two
devices ever write the same object.** File-level conflicts become impossible by construction.
Payloads are full state on the wire but applied **per record**, and merge rules are chosen so that
every device evaluates the same inputs to the same answer with zero coordination:

| Data class | Rule |
| --- | --- |
| Immutable facts | insert if unseen, never updated |
| Mutable metadata | whole-row last-write-wins on `updated_at` |
| Independently-created identities | merge by name, then converge on the lexicographically smallest uid |
| Deletes | tombstones, applied **first**, delete wins over any edit |

Merging is idempotent, which is what makes a dumb transport safe against retries and partial syncs.

### What not to copy from it

Three real defects found while studying it:

- Payloads are full state and rewritten on every publish, and tombstones are never pruned, so both
  grow without bound. Its Drive change-delta code exists but is never called, so every poll
  re-downloads every peer's entire history.

  An earlier version of this document concluded "we need a high-water mark from the start". That was
  **wrong, and trying it lost data.** A row merged from a peer carries *that peer's* `updated_at`, and
  peer clocks are independent, so a freshly-learned row often predates this device's own watermark —
  it is then never republished and never relayed, and a third device silently never receives it. The
  randomised soak test found this as devices holding different *sets* of transactions. Payloads are
  therefore full state here too. Doing it properly needs a **local** monotonic marker, bumped by
  merges as well as by user edits, which `updated_at` cannot be because it deliberately carries the
  originating device's time.
- On macOS its server-timestamp parameter is silently dropped on the polling path, so freshness falls
  back to peers' self-reported clocks — the exact skew the parameter was added to avoid.
- "Forget device" deletes a file rather than revoking anything; a signed-in device recreates it.

## 3. What this app has already

Better starting position than expected. Natural keys already exist on the fact tables, so those need
no minted uid at all — two devices that ingest the same provider record derive the same identity
without coordination:

| Table | Existing natural key | `updated_at` |
| --- | --- | --- |
| `transactions` | `(source, source_id)` unique | yes, with `onupdate` |
| `account_activity` | `(source, source_id)` unique | yes, with `onupdate` |
| `investment_period_facts` | `(source, period_end)` unique | no |
| `payslips` | `(source, signature)` unique | no |
| `retirement_transactions` | `(source, source_id)` unique | no |
| `retirement_statements` | `(source, period_end)` unique | no |

`transactions` is a **hybrid**, and this matters: the financial facts (date, amount, merchant) are
immutable, while `category`, `category_source`, `is_recurring`, `tags` and `notes` are user-edited. So
the row is inserted if unseen, and those annotation fields are last-write-wins on `updated_at`. The
money is never subject to LWW.

## 4. The sync set — and what is deliberately excluded

Chosen rather than "everything", because two of the exclusions are security boundaries.

**Synced today — provider facts** (insert if unseen; natural key above): `transactions`. That is the
only fact table with a `TableSpec`; check `src/sync/schema.py:TABLES` rather than this list, which is
prose and can drift.

**Intended but NOT yet synced.** These have the natural keys to make it straightforward and simply do
not have specs yet, so **their data does not travel between devices at all**:
`account_activity`, `investment_period_facts`, `payslips`, `payslip_line_items`,
`retirement_transactions`, `retirement_statements`.

This gap is worth stating loudly because **net-worth history and payroll are actively used**: until
these are added, a device only shows the payslips and investment facts it ingested itself, and net
worth will differ between devices even when transactions agree. An earlier version of this document
listed them as synced, which was wrong.

**Synced — user-authored** (need `uid` + `updated_at`; LWW, and merge-by-name for the first two):
`projects`, `contacts`, `subscriptions`, `rules`.

**Synced — links** (applied after their parents): `transaction_projects`, `project_members`,
`transaction_splits`, `transaction_project_splits`, `contact_splitwise_links`.

Link tables get **no uid**, which is a deliberate departure from Timeslice and specific to this
schema. Their primary keys are composites of local `id` columns, and those are per-device UUIDs — so
the composite key is *not* stable across devices even though it looks canonical. But every parent
already has a natural key (`projects.name` and `contacts.name` are `unique`; transactions have
`(source, source_id)`), so a link is expressed in the payload by **natural reference** —
`(transaction key, project name)`, `(project name, contact name)` — which is stable everywhere and
resolved to local ids on apply. Fewer new columns, less to backfill, and one less thing that can be
wrong on a live database.

They do get `updated_at`, because they carry edited values: `transaction_projects.description` and
the split shares are user input and need last-write-wins.

The cost of natural references is that renaming a project changes its identity. That is the same
trade Timeslice makes by merging on name, and it has the same failure mode: a rename that would
collide with an existing name cannot be applied. Renames are ordered by `updated_at` on the row's
`uid`, which is why `projects` and `contacts` keep a uid even though their names are the merge key —
the uid is what follows a project *through* a rename.

**Excluded — secrets and device-local state. These must never enter a payload:**

- **`sync_log`** — holds Plaid **access tokens** in `extra_data` and the per-item
  `plaid_cursor`. Both a secret and device-local position state. Syncing it would publish
  credentials to Drive and let two devices fight over one cursor.
- **`config.yaml`** — Plaid and Google client secrets. Note the *vault archive already includes it*;
  a sync payload must not. Different threat model: the vault is a restore-to-my-own-machine path.
- `plaid_api_usage` — per-device attribution; the whole point is that it is not shared.
- `app_metadata` — `database_instance_id` and the feedback counter are per-device by design.
- `exchange_rates` — regenerable from the provider; syncing it wastes payload for no gain.

**Undecided, deferred:** `balances`, `account_snapshots`, `investment_holding_snapshots`,
`source_balance_history`. These are provider observations at a point in time. They need natural keys
of the `(account_key, date)` shape before they can be exchanged, and `source_balance_history` already
has a partial unique index of exactly that form. Not in the first cut.

## 5. One syncer, not one timer

Timeslice's global invariant is "only one timer runs across all devices". Ours is **"only one device
runs Plaid sync"**, for three reasons: `transactions_sync` cursors are per-item and advancing one on
two devices independently is wasted work; the Plaid bill is per call; and the cursor lives in the
excluded `sync_log`, so it cannot be reconciled.

Same mechanism shape as Timeslice's takeover: an optimistic **claim with a heartbeat**, not a lock.
A device publishes a claim; a claim whose heartbeat has gone stale is treated as **live** rather than
dead when in doubt, because wrongly declaring a live syncer dead causes duplicate API calls, which is
worse than reacting late.

## 6. Phases

- **A — identity (implemented).** Additive nullable `uid` / `updated_at` columns, a `tombstones`
  table, a `sync_devices` registry. Columns are added by the existing `_ensure_transaction_columns`
  path; **minting values is a separate, idempotent CLI backfill** with a preview, never on startup.
- **B — change tracking.** Maintain `updated_at` on write, record tombstones on delete.
- **C — payload and merge engine**, with convergence tested as a property: merging two payloads in
  either order must reach the same state.
- **D — transport.** Per-device `device-<id>.json` in a `devices/` subfolder of the app's existing
  Drive folder, reusing the existing Drive client. **Drive is the only transport.** A directory-based
  one was added and then removed: a folder only reaches processes that can see that filesystem, so it
  is single-machine sync wearing the label of multi-device sync — a phone or a second Mac could never
  join. Tests use a dict-backed fake (`tests/sync_fakes.py`), which is a better double anyway. **Not** the appData folder: that needs the
  `drive.appdata` scope, and this app is authorised for `drive.file` only, so using it would force
  every existing user back through Google consent. `drive.file` covers files the app created, which
  is exactly what these are. Secrets excluded per §4.
- **E — the one-syncer claim.**
- **F — device registry UI**, showing which devices participate and when each was last seen.

Sync stays **off behind a flag** until C and D are proven against a copy of a real database. The
container app's behaviour does not change until then.

---

## 7. What has actually been exercised

`DriveTransport` **has been run against real Google Drive** (2026-09-15), with a synthetic payload
carrying zero records and a throwaway device id, deleted afterwards — so the plumbing is verified
without publishing any financial data. Nine checks passed: the `devices/` folder resolves, upload
succeeds, another device lists and downloads it, Drive reports a `modifiedTime`, **re-publishing
updates in place rather than creating a duplicate**, the update is what is read back, a device does not
fetch its own file, and delete removes it.

Still unexercised, and the honest gap: **a real payload over Drive between two apps has never run.**
Automated tests drive a fake transport, so `DriveTransport` itself has no automated coverage at all —
only the manual synthetic run above. Concurrency (two processes syncing at once) and clock skew are
also untested.

### Publishing is skipped when nothing changed

Payloads are full state, so republishing an unchanged one uploads the whole history — ~2.5 MB, which at
a 15-minute poll would be a few hundred megabytes a day for nothing. `run_sync` therefore compares a
cheap signature: per synced table the row count, the newest `updated_at`, **and the sum of all
`updated_at` values**.

The sum is not decoration. Count-and-maximum alone misses the most ordinary edit there is —
re-categorising an *old* transaction moves that row's timestamp but neither the count nor the table
maximum, so the change would never be published at all. That bug existed for one commit and was caught
by a test written for a different purpose.

An explicit "did the merge change anything?" condition was also present and was removed as provably
redundant: every synced model declares `onupdate` on `updated_at`, so any ORM modification moves a
timestamp the signature already covers.
