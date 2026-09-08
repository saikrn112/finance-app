#!/usr/bin/env bash
# Drive the *bundled* frontend against the *bundled* backend with Playwright.
#
# The plan's answer to "you can't screenshot the Mac UI" (§7): Playwright drives
# Chromium, not WKWebView, so this proves the frontend/backend contract and says
# nothing about the shell -- menus, window lifecycle, the OAuth return. That is
# where the expensive failures have been, and it is automatable, so it is worth
# having; it is not a substitute for driving the app by hand.
#
# Runs against a throwaway data directory. It never touches the real database.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

APP="$BUILD_DIR/$APP_NAME.app"
BACKEND="$APP/Contents/Resources/backend"
WEB="$APP/Contents/Resources/web"
VERIFY_DIR="$BUILD_DIR/verify"
LOG="$VERIFY_DIR/backend.log"
BACKEND_PID=""

cleanup() {
  if [ -n "$BACKEND_PID" ]; then
    kill -TERM "-$BACKEND_PID" 2>/dev/null || kill -TERM "$BACKEND_PID" 2>/dev/null || true
    sleep 1
    kill -KILL "-$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

[ -d "$APP" ] || die "no bundle at $APP -- run build_app.sh first"
[ -f "$WEB/index.html" ] || die "the bundle has no web UI; run 'npx vite build' in frontend/ then build_app.sh"

# Refuse to test a stale copy.
#
# This script serves the *bundle's* web assets, not frontend/dist, so running `vite build`
# alone changes nothing here. That is a silent failure: the suite passes against the
# previous build and the result looks like the change had no effect -- which is exactly what
# happened while auditing the palette.
if [ -f "$REPO_ROOT/frontend/dist/index.html" ] \
   && [ "$REPO_ROOT/frontend/dist/index.html" -nt "$WEB/index.html" ]; then
  die "frontend/dist is newer than the bundle's copy. Run: bash macos/scripts/build_app.sh"
fi

rm -rf "$VERIFY_DIR"
mkdir -p "$VERIFY_DIR/data/runtime/prod"

PY="$BACKEND/python/bin/python3"
PORT="$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(32))')"

# Demo mode by default, for two reasons.
#
# The obvious one: it seeds synthetic data, so the page walk exercises populated views
# rather than empty states, and no real financial data is ever involved.
#
# The load-bearing one: on a live database with no vault connection, `OnboardingGate`
# covers the whole app with a modal until Google Drive is connected, and there is no
# skip. Demo mode is the only supported way past it. See macos/README.md -- the gate is
# also a real problem for the app's own first run.
MODE="${FINANCE_APP_MODE:-demo}"

log "starting the bundled backend on 127.0.0.1:$PORT (mode: $MODE)"
env -i \
  PATH=/usr/bin:/bin \
  HOME="$HOME" \
  LANG=en_US.UTF-8 \
  PYTHONUNBUFFERED=1 \
  FINANCE_APP_MODE="$MODE" \
  FINANCE_APP_HOST=127.0.0.1 \
  FINANCE_APP_PORT="$PORT" \
  FINANCE_APP_LOCAL_TOKEN="$TOKEN" \
  FINANCE_APP_WEB_DIR="$WEB" \
  FINANCE_APP_CONFIG="$VERIFY_DIR/config.yaml" \
  FINANCE_APP_DATA_DIR="$VERIFY_DIR/data" \
  FINANCE_APP_RUNTIME_DIR="$VERIFY_DIR/data/runtime/prod" \
  FINANCE_APP_DB_PATH="$VERIFY_DIR/data/runtime/prod/finances.db" \
  /bin/sh -c "exec \"$PY\" -I \"$BACKEND/bootstrap.py\"" \
  > "$LOG" 2>&1 &
BACKEND_PID=$!

ready=0
for _ in $(seq 1 80); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$BACKEND_PID" 2>/dev/null || break
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  printf '\033[31mbackend never became ready\033[0m\n' >&2
  sed 's/^/    /' "$LOG" >&2
  exit 1
fi
log "ready"

log "running Playwright against the bundle"
set +e
( cd "$REPO_ROOT/frontend" \
  && BUNDLE_URL="http://127.0.0.1:$PORT" BUNDLE_TOKEN="$TOKEN" \
     npx playwright test --config=playwright.bundle.config.ts "$@" )
status=$?
set -e

if [ "$status" -ne 0 ]; then
  echo
  log "backend log tail (in case the failure was server-side):"
  tail -30 "$LOG" | sed 's/^/    /'
fi
exit "$status"
