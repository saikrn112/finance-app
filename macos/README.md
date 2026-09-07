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
  scripts/
    common.sh            shared settings; the bundle's dependency set lives here
    fetch_python.sh      download + cache python-build-standalone
    build_backend.sh     assemble build/backend (interpreter + wheels + app source)
    sign.sh              sign innermost-first, then the bundle
    spike_phase0.sh      phase 0 go/no-go checks
  payload/
    bootstrap.py         the bundled backend's entry point
    import_smoke.py      imports every native module; the real signing test
  Resources/
    Entitlements*.plist  see ENTITLEMENTS.md — no XML comments allowed in these
  build/                 gitignored
```

## Build the backend payload

```bash
bash macos/scripts/build_backend.sh      # ~2 min cold, most of it wheel downloads
bash macos/scripts/spike_phase0.sh       # end-to-end checks, throwaway data dir
```

`spike_phase0.sh` never touches `data/` — it runs against `macos/build/spike/data`.

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

## Test baselines

Compare before/after rather than expecting green (`AGENTS.md` §6).

| | Baseline at `4b847fd` | After phase 0 |
| --- | --- | --- |
| `pytest tests/ -q` | 61 failed, 134 passed | 61 failed, 136 passed |

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
- Launching the app bundle as a real GUI app — phase 0 has no shell yet.
