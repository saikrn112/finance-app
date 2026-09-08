#!/usr/bin/env bash
# Screenshot every page of the *real* app, in one appearance.
#
# Usage: screenshot_pages.sh [output-dir]
#
# Why this exists rather than relying on the Playwright visual audit: Playwright renders the
# page on a plain background in Chromium. It cannot show the window's opacity, its material,
# the traffic lights over the content, or the real surfaces behind translucent panels. A
# change that made the whole app look muddy over the desktop wallpaper passed that audit
# cleanly, because the audit could not see the thing that was wrong.
#
# So: launch the real bundle, walk it through every view with FINANCE_APP_DISPATCH, and
# capture the actual window each time. Progress is read from shell.log rather than by sleeping
# a guessed amount, so the captures line up with the navigation even on a slow machine.
#
# Needs Screen Recording granted to the terminal (see screenshot_app.sh).

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

OUT="${1:-$BUILD_DIR/screenshots/pages-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"

APP="$BUILD_DIR/$APP_NAME.app"
[ -d "$APP" ] || die "no bundle at $APP -- run build_app.sh first"

# Privacy mode is turned on before anything is captured, and the backend runs with its own
# mask as well. Two layers on purpose: `toggle:privacy` hides figures in the UI, while
# FINANCE_APP_PRIVACY_MASK replaces them in the API responses, so no real amount exists in the
# page to be captured in the first place. These screenshots get shared.
VIEWS=(
  "toggle:privacy"
  "navigate:home"
  "navigate:net-worth"
  "navigate:projects"
  "navigate:recurring"
  "navigate:uncategorized"
  "navigate:payroll"
  "navigate:investments"
  "navigate:retirement"
)
# Comfortably longer than a capture takes (~2s), because the tour advances on its own timer
# while this script screenshots. At 2.5s the two desynced and every image after the third was
# labelled with the wrong view.
INTERVAL=8

cleanup() {
  pkill -f "$APP_NAME.app/Contents/MacOS/$APP_NAME" 2>/dev/null || true
  sleep 1
  pkill -9 -f 'backend/python/bin/python3 -I' 2>/dev/null || true
}
trap cleanup EXIT
cleanup

LOG="$HOME/Library/Logs/$APP_NAME/shell.log"
: > "$LOG" 2>/dev/null || true

log "launching with a dispatch tour"
# Launched directly rather than via `open`, which provides no way to set the environment.
FINANCE_APP_DISPATCH="$(IFS=,; echo "${VIEWS[*]}")" \
FINANCE_APP_DISPATCH_INTERVAL="$INTERVAL" \
FINANCE_APP_PRIVACY_MASK=1 \
  "$APP/Contents/MacOS/$APP_NAME" >/dev/null 2>&1 &

# Wait for the first paint.
for _ in $(seq 1 80); do
  grep -q "auth self-check" "$LOG" 2>/dev/null && break
  sleep 0.5
done
grep -q "auth self-check" "$LOG" 2>/dev/null || die "the app never became ready; see $LOG"

# No pre-tour capture: the first dispatched command is `toggle:privacy`, and a screenshot
# taken before it lands would show real figures.
index=1
for view in "${VIEWS[@]}"; do
  # Wait for non-navigation commands (the privacy toggle) but do not capture them.
  case "$view" in
    navigate:*) ;;
    *)
      for _ in $(seq 1 60); do
        grep -q "dispatched '$view'" "$LOG" 2>/dev/null && break
        sleep 0.25
      done
      log "applied $view"
      continue
      ;;
  esac
  name="${view#navigate:}"
  # Wait for the shell to report *this* dispatch, then let the page settle. Reading the log
  # beats sleeping a fixed total: a slow first render would otherwise shift every capture.
  for _ in $(seq 1 60); do
    grep -q "dispatched '$view'" "$LOG" 2>/dev/null && break
    sleep 0.25
  done
  sleep 1.2
  padded="$(printf '%02d' "$index")"
  bash "$MACOS_DIR/scripts/screenshot_app.sh" "$OUT/$padded-$name.png" >/dev/null \
    || warn "could not capture $name"
  # Check the app had not already moved on. A mislabelled screenshot is worse than a missing
  # one: it sends you looking for a defect on the wrong page.
  last="$(grep -o "dispatched 'navigate:[a-z-]*'" "$LOG" | tail -1)"
  [ "$last" = "dispatched '$view'" ] \
    || warn "$name may be mislabelled: the app had advanced to $last"
  index=$((index + 1))
done

log "captured $(ls "$OUT" | wc -l | tr -d ' ') screenshots in $OUT"
ls "$OUT"
