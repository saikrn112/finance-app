#!/usr/bin/env bash
# Bring FinanceApp forward and screenshot just its window.
#
# Usage: screenshot_app.sh [output.png]
#
# Needs one TCC grant for the terminal running this, which only a human can give:
# **Screen Recording** (System Settings -> Privacy & Security).
#
# How the denial actually presents, because it is misleading in both directions:
#   * `-l <window-id>` and `-R <rect>` fail outright, with "could not create image from
#     window" / "from rect" -- which reads like a broken window id or a macOS version
#     problem, not a permissions one. The window id resolves fine either way.
#   * a plain whole-screen `screencapture -x` *succeeds* and writes a perfectly valid PNG
#     of the desktop with no windows in it (~100KB). So neither the exit status nor the
#     file size tells you the grant is missing.
# The only reliable check is content: capture, hide the window, capture again, and compare
# hashes. Identical bytes mean windows are not being captured at all.
#
# The grant is per-host-application and is lost when that application changes, so a tour
# that worked last week can fail today with nothing in this repo having changed.
#
# Deliberately does *not* need Accessibility. Reading a window's bounds through System
# Events is UI scripting and requires it; asking CoreGraphics for the window list does
# not. `screencapture -l <window-id>` then captures exactly that window whatever is in
# front of it -- which also makes the capture reliable rather than "whatever was frontmost",
# and a plain `screencapture -x` during an agent session is usually the terminal.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

OUT="${1:-$BUILD_DIR/screenshots/$(date +%Y%m%d-%H%M%S).png}"
mkdir -p "$(dirname "$OUT")"

pgrep -f "$APP_NAME.app/Contents/MacOS/$APP_NAME" >/dev/null \
  || die "$APP_NAME is not running"

# Build the helper once. swiftc is fast enough here and beats `swift file.swift`, which
# recompiles on every call.
HELPER="$CACHE_DIR/window_id"
SOURCE="$MACOS_DIR/scripts/tools/window_id.swift"
if [ ! -x "$HELPER" ] || [ "$SOURCE" -nt "$HELPER" ]; then
  mkdir -p "$CACHE_DIR"
  swiftc -O -o "$HELPER" "$SOURCE"
fi

# Activating does not require Accessibility, and makes the capture show the app as the
# user would see it (focused, with active-state controls).
osascript -e "tell application id \"$BUNDLE_ID\" to activate" >/dev/null 2>&1 || true
sleep "${SCREENSHOT_SETTLE:-1.2}"

window="$("$HELPER" "$APP_NAME")" || die "could not find $APP_NAME's window"

# -o omits the window shadow, so the image is the window rather than a soft-edged
# rectangle over whatever is behind it.
screencapture -x -o -l "$window" "$OUT"

[ -s "$OUT" ] || die "screencapture produced nothing -- is Screen Recording granted?"
log "captured window $window -> $OUT"
echo "$OUT"
