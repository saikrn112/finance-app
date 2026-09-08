#!/usr/bin/env bash
# Assemble and sign FinanceApp.app.
#
# Usage: build_app.sh [debug|release]
#
# Set MACOS_SIGN_IDENTITY to a Developer ID to get a notarizable bundle; the
# default is ad-hoc, which is fine for development and breaks TCC grants, Keychain
# ACLs and notarization (see Resources/ENTITLEMENTS.md).
#
# NSSupportsSuddenTermination is false in Info.plist so applicationWillTerminate
# actually runs and the backend is killed rather than leaked.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CONFIGURATION="${1:-debug}"
case "$CONFIGURATION" in
  debug|release) ;;
  *) die "unknown configuration: $CONFIGURATION (want debug or release)" ;;
esac

APP="$BUILD_DIR/$APP_NAME.app"
CONTENTS="$APP/Contents"

# ------------------------------------------------------------------- the shell --
log "building the Swift shell ($CONFIGURATION)"
( cd "$MACOS_DIR" && swift build -c "$CONFIGURATION" )
BINARY="$(cd "$MACOS_DIR" && swift build -c "$CONFIGURATION" --show-bin-path)/$APP_NAME"
[ -x "$BINARY" ] || die "shell binary not found: $BINARY"

# ----------------------------------------------------------------- the backend --
if [ ! -f "$BUILD_DIR/backend/bootstrap.py" ] || [ "${FORCE_BACKEND:-0}" = "1" ]; then
  bash "$MACOS_DIR/scripts/build_backend.sh"
else
  log "reusing existing backend payload (FORCE_BACKEND=1 to rebuild)"
fi

# ------------------------------------------------------------------- the bundle --
log "assembling $APP"
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

cp "$BINARY" "$CONTENTS/MacOS/$APP_NAME"
cp "$MACOS_DIR/Resources/Info.plist" "$CONTENTS/Info.plist"
printf 'APPL????' > "$CONTENTS/PkgInfo"

# The app icon. Regenerated when the generator changes, so the committed .icns and the
# code that draws it cannot drift apart.
ICON_SOURCE="$MACOS_DIR/scripts/make_icon.swift"
ICON_SET="$MACOS_DIR/Resources/AppIcon.iconset"
ICON="$MACOS_DIR/Resources/AppIcon.icns"
if [ ! -f "$ICON" ] || [ "$ICON_SOURCE" -nt "$ICON" ]; then
  log "regenerating the app icon"
  ( cd "$REPO_ROOT" && swift "$ICON_SOURCE" >/dev/null )
  iconutil -c icns "$ICON_SET" -o "$ICON"
fi
cp "$ICON" "$CONTENTS/Resources/AppIcon.icns"
cp -R "$BUILD_DIR/backend" "$CONTENTS/Resources/backend"

# The built frontend. Absent until phase 2; the shell shows the backend status
# window in that case rather than a blank webview.
if [ -d "$REPO_ROOT/frontend/dist" ]; then
  cp -R "$REPO_ROOT/frontend/dist" "$CONTENTS/Resources/web"
  log "included frontend/dist as Resources/web"
else
  warn "no frontend/dist -- bundle has no web UI yet (phase 2)"
fi

# --------------------------------------------------------------------- signing --
bash "$MACOS_DIR/scripts/sign.sh" "$APP" "${MACOS_SIGN_IDENTITY:--}"

log "built: $APP ($(du -sh "$APP" | cut -f1))"
log "run it with: open '$APP'"
