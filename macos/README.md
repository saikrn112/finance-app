# macOS desktop app

A native AppKit shell that hosts the existing React frontend in a `WKWebView` and
runs the existing FastAPI backend as an embedded child process on `127.0.0.1`.

The design rationale — and why a SwiftUI rewrite or Electron/Tauri was rejected —
lives in the plan at
`~/workspace/persona/Notes/Projects/finance_app/artifacts/macos_desktop_app_plan.md`.
Read the repo's `AGENTS.md` too; its hard rules apply here unchanged.

---

## Layout

```
macos/
  Package.swift
  Sources/
    FinanceCore/         UI-free logic: process lifecycle, paths, policy. Testable.
    FinanceApp/          the AppKit shell
  Tests/FinanceCoreTests/
  scripts/
    common.sh            shared settings; the bundle's dependency set lives here
    fetch_python.sh      download + cache python-build-standalone
    build_backend.sh     assemble build/backend (interpreter + wheels + app source)
    build_app.sh         build the shell, assemble FinanceApp.app, sign it
    sign.sh              sign innermost-first, then the bundle
    spike_phase0.sh      phase 0 go/no-go checks
  payload/
    bootstrap.py         the bundled backend's entry point
    import_smoke.py      imports every native module; the real signing test
  Resources/
    Info.plist
    Entitlements*.plist  see ENTITLEMENTS.md — no XML comments allowed in these
  build/                 gitignored
```

Everything that can be decided without a window lives in `FinanceCore`, because
verifying macOS UI headlessly is largely impossible (plan §7). `FinanceApp` is meant to
stay thin.

## Build and run

```bash
bash macos/scripts/build_backend.sh      # ~2 min cold, most of it wheel downloads
bash macos/scripts/spike_phase0.sh       # phase 0 checks, throwaway data dir
cd frontend && npx vite build            # NOT `npm run build`: that runs `tsc -b`
                                         # first, which fails on the 59 pre-existing
                                         # errors. vite build does not typecheck.
bash macos/scripts/build_app.sh          # -> macos/build/FinanceApp.app
open macos/build/FinanceApp.app

cd macos && swift test                   # 61 tests
bash macos/scripts/verify_bundle.sh      # Playwright against the bundle
```

`spike_phase0.sh` never touches `data/` — it runs against `macos/build/spike/data`.
The app itself writes only to `~/Library/Application Support/FinanceApp` and
`~/Library/Logs/FinanceApp`.

`FORCE_BACKEND=1` rebuilds the Python payload, which `build_app.sh` otherwise reuses.

## Signing

Following what Timeslice does (`~/workspace/persona/timeslice/scripts/`): **not
sandboxed**, ad-hoc by default, with an optional stable self-signed identity for people
who rebuild often. `Entitlements.plist` states `app-sandbox: false` explicitly rather than
by omission, so the intent is on the record — the app spawns a child interpreter and reads
a user-chosen plugins directory, and the sandbox forbids both as designed here. No App
Store.

```bash
bash macos/scripts/build_app.sh                              # ad-hoc
MACOS_SIGN_IDENTITY="FinanceApp Local" bash macos/scripts/build_app.sh
```

An ad-hoc signature changes on every build, and macOS keys TCC grants and Keychain ACLs to
the signature — so every rebuild re-prompts. A self-signed certificate created once in
Keychain Access (Certificate Assistant → Create a Certificate, type *Code Signing*, named
`FinanceApp Local`) fixes that, and is the same trick Timeslice uses.

It does **not** make the app notarizable, and it does not have a Team ID — so
`sign.sh` still gives the bundled interpreter the permissive entitlements. Only a real
`Developer ID Application` certificate takes the strict path; see
`Resources/ENTITLEMENTS.md`.

---

## Phase 0 result: GO

Verified on macOS 26.6.2, arm64, Xcode 26.6, Swift 6.3.3.

| Go criterion | Result |
| --- | --- |
| Every wheel is pure-Python or macOS arm64 | 31/31 |
| Server starts under `-I` and `/api/health` answers | pass |
| Isolated from the user's Python | pass (a planted `PYTHONPATH` shadow had no effect) |
| Loopback token gate rejects unauthenticated `/api` | pass (401 / 401 / 200) |
| `codesign --verify --deep --strict` | pass |
| All native modules import from the **signed** bundle | 26/26 |

Payload: ~150 MB, 41 native binaries to sign.

### What phase 0 changed in core

Two things the wrapping needs that the repo could not express. Both are generic
infrastructure and no-ops for the container and local dev flows.

1. **`FINANCE_APP_CONFIG`** (`src/config.py`). `Settings.load()` resolved
   `config.yaml` relative to the working directory only, so a bundled backend
   could never find a config in `~/Library/Application Support`. The
   `config.yaml.example` fallback now also resolves from the project root rather
   than the CWD.
2. **`FINANCE_APP_LOCAL_TOKEN`** (`src/api/local_auth.py`). Any process running as
   this user can reach `127.0.0.1:<port>`, and the API exposes complete financial
   history. The shell generates a token per launch; the gate installs itself only
   when the variable is set. Exemptions are exact paths, not prefixes, and exist
   only for provider OAuth redirects that arrive via the system browser and so can
   carry neither our header nor our cookie.

### Decisions taken, with the plan's recommended defaults

- **arm64 only.** Intel means universal2 wheels for every native package or a
  second architecture slice (plan §10 q1).
- **`uvicorn`, not `uvicorn[standard]`.** Drops `uvloop`, `httptools` and
  `watchfiles`: three compiled wheels and three signing surfaces, bought for a
  performance win that is irrelevant for one local user on SQLite.
- **`google-generativeai` is not bundled**, so `grpcio` and `protobuf` are not
  either. The categorizer already imports it lazily, so AI categorisation reads as
  unavailable rather than failing (plan §10 q2). `pyproject.toml` is unchanged —
  the omission is in `common.sh`'s `BUNDLE_REQUIREMENTS`, so the dev environment
  keeps the feature.
- **Tcl/Tk, pip, setuptools and the console-script wrappers are stripped** from the
  interpreter. Not for size: each removed dylib is one less binary to sign, verify
  and notarize.

### Things phase 0 learned the hard way

- **`pdfplumber` no longer pulls in `pycryptodome`.** The plan's dependency table
  is out of date: the current chain is `pdfminer.six` + `pypdfium2` +
  `cryptography` (Rust) + `Pillow`. Same conclusion, different binaries.
- **`plaid-python` publishes no wheel**, only an sdist. Harmless — it's pure
  Python, so `build_backend.sh` builds it into a `py3-none-any` wheel on the build
  machine. The plan's "an sdist means it needs a compiler on the user's machine"
  only bites for *native* packages, which is what the wheelhouse audit checks.
- **`-I` implies `-E`.** The plan's "run with `-I` and set `PYTHONHOME`/`PYTHONPATH`"
  is self-defeating: `-E` makes the interpreter ignore every `PYTHON*` variable.
  `bootstrap.py` builds `sys.path` in code instead, which is strictly better —
  it cannot be shadowed by the user's environment at all.
- **Ad-hoc + hardened runtime cannot load any bundled extension module.** See
  `Resources/ENTITLEMENTS.md` for the measurement and the dev-only workaround.
  This is the single most useful thing `import_smoke.py` buys: `codesign --verify`
  passed on a bundle in which every native import failed.
- **`codesign` rejects XML comments in entitlements** (`AMFIUnserializeXML: syntax
  error`), even though `plutil -lint` accepts the file.

---

## Phase 1 result: shell + backend lifecycle works

A window showing backend state, with the port allocator, per-launch token, process
group, crash restart with backoff, log rotation, single-instance guard and clean
shutdown behind it. No webview yet.

Verified by driving the built bundle, not by reading the code:

| Behaviour | How it was checked | Result |
| --- | --- | --- |
| Backend starts and serves | launch, read the port from the log | ready in <10 s |
| Token gate is live | `curl` each endpoint with no token | `/api/health` 200; `/api/meta`, `/api/transactions`, `/api/settings/plaid/accounts` all 401 |
| Own process group | `ps -o pgid=` on child vs shell | child pgid == its own pid, ≠ shell's |
| Crash restart | `kill -9` the backend | new pid ~4 s later, on a freshly allocated port |
| Clean quit | AppleScript `quit` | shell and backend both gone, `runtime-state.json` cleared |
| SIGTERM to the shell | `pkill` the shell | backend also gone (see below) |
| Crash recovery | `kill -9` the *shell*, then relaunch | orphan swept, one fresh backend, no duplicate |
| Single-instance guard | `open -n` a second copy | second refuses with an alert and starts no backend |

**Not verified:** the window's appearance and layout. There is no reliable way to
screenshot the Mac UI headlessly (plan §11.8, and `screencapture` needs a TCC grant), so
`BackendStatusView` has been compiled and driven but not *seen*. The states it renders
are all covered by `swift test`; the pixels are not.

### Two bugs found by running it, both invisible in review

1. **AppKit does not turn a signal into `applicationWillTerminate`.** `pkill` on the
   shell left a live uvicorn holding the SQLite file. Fixed with
   `TerminationSignalHandler` (SIGTERM/SIGINT/SIGHUP → stop the backend, then exit).
   `StaleBackendSweeper` remains the backstop, since `SIGKILL` cannot be caught.

2. **The backend inherited the single-instance `flock` descriptor**, which made the app
   *permanently unlaunchable after one crash*. `SIGKILL` of the shell meant `release()`
   never ran, and the inherited descriptor kept the open file description — and so the
   lock — alive. Because the guard runs before the stale-backend sweep, every later
   launch was refused with "already running" before it could clean up. Fixed at the
   descriptor level, in both places that could leak one: `O_CLOEXEC` on the lock and
   `POSIX_SPAWN_CLOEXEC_DEFAULT` on the spawn.

   Both fixes have tests that were confirmed to **fail when the fix is reverted**. The
   first attempt at the second test did not: it called `release()` explicitly, and
   `flock` is keyed to the shared open file description, so the release dropped the lock
   for the child too and the test passed either way. A test that passes without the fix
   is worse than no test.

Also worth recording: an AppKit app with no main menu has no ⌘Q, so the only way to quit
is to kill the process — which is exactly the path that orphans the backend. A minimal
menu bar is therefore part of phase 1, not phase 3.

---

## Phase 2 result: the app appears

`vite build` output ships in `Resources/web`, FastAPI serves it from its own origin, and
a `WKWebView` loads `http://127.0.0.1:<port>/` with the session token already in its
cookie store.

Being same-origin removes three things at once: CORS, the Vite proxy, and the
`vite.config.ts` env-var trap that made every `/api` call fail on a non-default port
(`AGENTS.md` caveat #9). The frontend needed **no changes** — `api.ts` already uses a
relative `/api` base, and there is no router, so no SPA fallback was required.

### Verified

`macos/scripts/verify_bundle.sh` runs Playwright against the *bundled* frontend and the
*bundled* backend — no mocks, no dev server (12 passed, 2 skipped):

- `/` serves the built `index.html`; every `/assets/*` returns 200.
- The API is not shadowed by the static mount (which is why it is registered last).
- The Google OAuth callback still wins at `/`, because it is identified by its query
  parameters rather than by a distinct path.
- Page traffic carries the token; a request with no token gets 401 while `/` still
  returns 200. The gate's cookie path is covered too, even though the shell no longer
  uses it.
- The token appears nowhere in the served HTML.
- Home, Projects, Uncategorized, Payroll, Imports and Settings each render with no
  uncaught error, no 401 and no 5xx.

Driving the real bundle, with `shell.log` as the observable:

```
launching
backend ready on port 62656; showing the app
webview loaded http://127.0.0.1:62656
backend no longer ready; showing the status view      <- kill -9 on the backend
backend ready on port 62733; showing the app
webview loaded http://127.0.0.1:62733
```

The webview is rebuilt rather than reloaded on restart, because the port *and* the token
both change; a reload would keep a cookie for a token that no longer exists.

### The bug that mattered: the app looked fine and authenticated nothing

The token first travelled as a cookie in the webview's data store. `HTTPCookie`
construction succeeded, `setCookie`'s completion fired, reading the store back showed the
cookie present — and WKWebView never attached it to a request to
`http://127.0.0.1:<port>`. Neither `.domain` nor `.originURL` helped. Every `/api` call
got 401.

**It was invisible.** The window rendered, the layout was right, and every panel read
`$0.00` — which on an empty database is also what success looks like. The onboarding gate
never appeared either, because the settings request 401ed too, so the one symptom that
would have given it away was suppressed by the same bug. `verify_bundle.sh` was green
throughout, because Playwright has its own cookie jar and its own idea of
domain-matching.

What found it: a screenshot from the user, and then
`checkAuthenticatedFromInsideTheWebview()` — a `callAsyncJavaScript` probe that asks the
*page* to `fetch('/api/meta')` and logs the status on every load. That check stays, because
this failure mode has no other outward sign. (`evaluateJavaScript` cannot await a promise;
it returns the `Promise` object and reports "a result of an unsupported type".)

The fix is a `WKUserScript` at `.atDocumentStart` that wraps `fetch` and `XMLHttpRequest`
to add `x-finance-token` on same-origin requests. The harness now authenticates the same
way, because **a harness that authenticates differently from the app cannot catch the app's
auth bugs** — that mismatch is the entire reason this survived a green test suite.

### Two further findings, one of them not ours

1. **First run is blocked by `OnboardingGate`, and its Google flow cannot work in a
   webview.** On a live database with no vault connection the gate covers the whole app
   until Google Drive is connected, and there is no skip — reproduced twice against a fresh
   live-mode database. Its connect flow uses `window.open` plus
   `window.opener.postMessage`, but the shell sends off-origin navigation to the system
   browser, so there is no `opener` to post back to. This is phase 4 work (a custom URL
   scheme and a real return path); until then the bundle can only be *driven* in `demo`
   mode, which is why `verify_bundle.sh` defaults to it.

   Worth noting how close this came to hiding the token bug above: while nothing
   authenticated, the gate did not appear either, and the app looked more usable than it
   was.

2. **A pre-existing race in `_ensure_identity_rates`** (`src/services/exchange_rates.py`)
   returns 500 to concurrent callers: each sees the identity rates missing and each
   inserts them, and the losers get `UNIQUE constraint failed: exchange_rates.…` from a
   query-invoked autoflush. Reproduced on an **unmodified `main` at 4b847fd**: 11 of 12
   concurrent `/api/analytics/summary` requests returned 500. It affects the web app too
   and is **not fixed here** — out of scope, and it is in the money layer where this
   repo's rules are strictest. `verify_bundle.sh` warms the rates with one sequential
   request first, so the suite's 5xx assertions can stay strict.

### And two Swift traps worth knowing

`WKNavigationDelegate`'s `decidePolicyFor` takes a `@MainActor @Sendable` completion. A
near-miss signature **compiles**, emits only a "nearly matches optional requirement"
warning, and is never called — which would have left off-origin navigation completely
unenforced while looking implemented. Build with zero warnings here; that one is
load-bearing.

`ShellLog.write` shadowed `Darwin.write(2)` inside its own body. The compiler catches this
one, unlike the others.

---

## Test baselines

Compare before/after rather than expecting green (`AGENTS.md` §6).

| | Baseline at `4b847fd` | Now |
| --- | --- | --- |
| `pytest tests/ -q` | 61 failed, 134 passed | 61 failed, 147 passed |
| `npx tsc -p tsconfig.app.json --noEmit` | 59 errors | 59 errors |
| `swift test` (new) | — | 61 passed |
| `macos/scripts/verify_bundle.sh` (new) | — | 13 passed, 2 skipped |

Frontend typecheck is untouched so far; record it before the first
`frontend/` change:

```bash
cd frontend && npx tsc -p tsconfig.app.json --noEmit 2>&1 | grep -c 'error TS'
```

## Not yet verified

- Anything requiring a **Developer ID**: notarization, stapling, and whether the
  release entitlements (empty) suffice. This machine reports 0 codesigning
  identities.
- The **release** interpreter entitlements path in `sign.sh`.
- **How the window looks.** See the phase 1 note above — the states are tested, the
  pixels are not.
- Sleep/wake: `NSWorkspace.didWakeNotification` is wired to `revalidate()`, but the
  machine has not actually been slept with the app running.
