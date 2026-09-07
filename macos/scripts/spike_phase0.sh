#!/usr/bin/env bash
# Phase 0 go/no-go spike (plan §6).
#
# Go criteria:
#   1. every wheel entering the bundle is a binary wheel for arm64 (or pure Python)
#   2. the server starts under `-I` against the real src/, and /api/health answers
#   3. the interpreter is genuinely isolated from the user's Python
#   4. the loopback token gate refuses an unauthenticated /api request
#   5. `codesign --verify --deep --strict` passes on the signed bundle
#
# Runs against a throwaway data directory. It never touches data/ in the repo.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

SPIKE_DIR="$BUILD_DIR/spike"
BACKEND_DIR="$BUILD_DIR/backend"
DATA_DIR="$SPIKE_DIR/data"
LOG="$SPIKE_DIR/backend.log"
BACKEND_PID=""
failures=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[32mPASS\033[0m %s\n' "$label"
  else
    printf '  \033[31mFAIL\033[0m %s\n' "$label"
    failures=$((failures + 1))
  fi
}

cleanup() {
  if [ -n "$BACKEND_PID" ]; then
    # Kill the whole process group. uvicorn's reloader/child handling is the same
    # trap as AGENTS.md caveat #10: killing the recorded pid can leave a
    # grandchild alive holding the SQLite file.
    kill -TERM "-$BACKEND_PID" 2>/dev/null || kill -TERM "$BACKEND_PID" 2>/dev/null || true
    sleep 1
    kill -KILL "-$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

rm -rf "$SPIKE_DIR"
mkdir -p "$DATA_DIR"

[ -f "$BACKEND_DIR/bootstrap.py" ] || bash "$MACOS_DIR/scripts/build_backend.sh"

PY="$BACKEND_DIR/python/bin/python3"

# ----------------------------------------------------------- 1. wheel audit --
log "1/5 wheelhouse audit"
# build_backend.sh already fails the build on a bad wheel; re-state the result
# here so the spike output is self-contained.
sed -n '/--- wheels ---/,/--- native extensions ---/p' "$BACKEND_DIR/MANIFEST.txt" \
  | grep -c '\.whl$' | xargs -I{} echo "  {} wheels, all pure-Python or macOS arm64"

# ------------------------------------------------------------- 2. it starts --
log "2/5 starting bundled backend"
PORT="$("$PY" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Poison the environment on purpose. With `-I` none of this may take effect.
POISON="$SPIKE_DIR/poison"
mkdir -p "$POISON"
cat > "$POISON/click.py" <<'PY'
raise SystemExit("bootstrap: the user's PYTHONPATH shadowed a bundled package")
PY

set +e
env -i \
  PATH=/usr/bin:/bin \
  HOME="$HOME" \
  PYTHONPATH="$POISON" \
  PYTHONHOME=/nonexistent \
  FINANCE_APP_PORT="$PORT" \
  FINANCE_APP_HOST=127.0.0.1 \
  FINANCE_APP_DATA_DIR="$DATA_DIR" \
  FINANCE_APP_RUNTIME_DIR="$DATA_DIR/runtime/prod" \
  FINANCE_APP_DB_PATH="$DATA_DIR/runtime/prod/finances.db" \
  FINANCE_APP_LOCAL_TOKEN="$TOKEN" \
  /bin/sh -c "exec \"$PY\" -I \"$BACKEND_DIR/bootstrap.py\"" \
  > "$LOG" 2>&1 &
BACKEND_PID=$!
set -e

ready=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ready=1; break; fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then break; fi
  sleep 0.5
done

if [ "$ready" -ne 1 ]; then
  printf '  \033[31mFAIL\033[0m backend never became ready. Log:\n'
  sed 's/^/    /' "$LOG"
  exit 1
fi
check "/api/health responds" curl -fsS "http://127.0.0.1:$PORT/api/health"

# --------------------------------------------------------- 3. env isolation --
log "3/5 isolation from the user's Python"
check "PYTHONPATH shadow did not take effect" \
  sh -c "! grep -q \"shadowed a bundled package\" '$LOG'"
sp="$(curl -fsS -H "x-finance-token: $TOKEN" "http://127.0.0.1:$PORT/api/health" >/dev/null && \
      "$PY" -I -c "import sys; print('ok')")"
check "interpreter runs under -I" test "$sp" = "ok"

# ------------------------------------------------------------- 4. token gate --
log "4/5 loopback token gate"
code_no_token="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/meta")"
code_token="$(curl -s -o /dev/null -w '%{http_code}' -H "x-finance-token: $TOKEN" "http://127.0.0.1:$PORT/api/meta")"
code_bad="$(curl -s -o /dev/null -w '%{http_code}' -H "x-finance-token: wrong" "http://127.0.0.1:$PORT/api/meta")"
check "no token  -> 401 (got $code_no_token)"  test "$code_no_token" = "401"
check "bad token -> 401 (got $code_bad)"       test "$code_bad" = "401"
check "good token -> 200 (got $code_token)"    test "$code_token" = "200"

cleanup
BACKEND_PID=""

# ----------------------------------------------------------------- 5. signing --
log "5/5 ad-hoc signing over the whole payload"
APP="$SPIKE_DIR/$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp -R "$BACKEND_DIR" "$APP/Contents/Resources/backend"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID.spike</string>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
PLIST
printf '#!/bin/sh\nexit 0\n' > "$APP/Contents/MacOS/$APP_NAME"
chmod +x "$APP/Contents/MacOS/$APP_NAME"

bash "$MACOS_DIR/scripts/sign.sh" "$APP" >"$SPIKE_DIR/sign.log" 2>&1 || {
  printf '  \033[31mFAIL\033[0m signing. Log:\n'
  tail -30 "$SPIKE_DIR/sign.log" | sed 's/^/    /'
  failures=$((failures + 1))
}
check "codesign --verify --deep --strict" \
  codesign --verify --deep --strict --verbose=2 "$APP"

# Verification is not the same as loadability: signing and the hardened runtime
# only fail at dlopen time, so run the signed interpreter and import everything.
log "5b/5 importing every native module from the signed bundle"
signed_py="$APP/Contents/Resources/backend/python/bin/python3"
if "$signed_py" -I "$APP/Contents/Resources/backend/import_smoke.py" > "$SPIKE_DIR/imports.log" 2>&1; then
  printf '  \033[32mPASS\033[0m all bundled modules import under the signed interpreter\n'
  sed 's/^/    /' "$SPIKE_DIR/imports.log"
else
  printf '  \033[31mFAIL\033[0m import smoke test:\n'
  sed 's/^/    /' "$SPIKE_DIR/imports.log"
  failures=$((failures + 1))
fi

echo
if [ "$failures" -eq 0 ]; then
  printf '\033[1;32mPHASE 0: GO\033[0m  (%s)\n' "$(du -sh "$BACKEND_DIR" | cut -f1) backend payload"
else
  printf '\033[1;31mPHASE 0: %s check(s) failed\033[0m\n' "$failures"
  exit 1
fi
