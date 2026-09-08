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
bash macos/scripts/build_app.sh          # -> macos/build/FinanceApp.app
open macos/build/FinanceApp.app

cd macos && swift test                   # 61 tests
```

`spike_phase0.sh` never touches `data/` — it runs against `macos/build/spike/data`.
The app itself writes only to `~/Library/Application Support/FinanceApp` and
`~/Library/Logs/FinanceApp`.

Set `MACOS_SIGN_IDENTITY` to build with a Developer ID; the default is ad-hoc.
`FORCE_BACKEND=1` rebuilds the Python payload, which `build_app.sh` otherwise reuses.

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

## Test baselines

Compare before/after rather than expecting green (`AGENTS.md` §6).

| | Baseline at `4b847fd` | Now |
| --- | --- | --- |
| `pytest tests/ -q` | 61 failed, 134 passed | 61 failed, 136 passed |
| `swift test` (new) | — | 61 passed |

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
