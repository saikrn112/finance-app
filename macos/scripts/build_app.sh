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

CONFIGURATION="${1:-release}"
case "$CONFIGURATION" in
  debug|release) ;;
  *) die "unknown configuration: $CONFIGURATION (want debug or release)" ;;
esac

APP="$BUILD_DIR/$APP_NAME.app"
CONTENTS="$APP/Contents"

# ------------------------------------------------------------------- the shell --
log "building the Swift shell ($CONFIGURATION)"
( cd "$MACOS_DIR" && swift build --disable-sandbox -c "$CONFIGURATION" -Xswiftc -gnone -Xswiftc -file-prefix-map -Xswiftc "$REPO_ROOT=/FinanceSource" )
BINARY="$(cd "$MACOS_DIR" && swift build -c "$CONFIGURATION" --show-bin-path)/FinanceApp"
[ -x "$BINARY" ] || die "shell binary not found: $BINARY"

# ----------------------------------------------------------------- the backend --
if [ ! -f "$BUILD_DIR/backend/bootstrap.py" ] || [ "${FORCE_BACKEND:-0}" = "1" ] || [ "$(cat "$BUILD_DIR/backend/DEPENDENCIES.sha256" 2>/dev/null || true)" != "$DEPENDENCY_FINGERPRINT" ]; then
  bash "$MACOS_DIR/scripts/build_backend.sh"
else
  # The interpreter and the wheelhouse are reused -- they take minutes and rarely change.
  # The app's own Python is re-copied unconditionally, because it changes constantly and
  # caching it shipped stale code: a route added in the repo 404ed in the bundle with nothing
  # to explain it, and `FORCE_BACKEND=1` was the workaround for a wrong default.
  log "reusing the cached interpreter and wheels (FORCE_BACKEND=1 to rebuild them)"
  bash "$MACOS_DIR/scripts/sync_app_source.sh"
fi

# ------------------------------------------------------------------- the bundle --
log "assembling $APP"
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

cp "$BINARY" "$CONTENTS/MacOS/FinanceApp"
if [ "$CONFIGURATION" = "release" ]; then strip -S "$CONTENTS/MacOS/FinanceApp"; fi
cp "$MACOS_DIR/Resources/Info.plist" "$CONTENTS/Info.plist"
if [ "${FINANCE_DEMO_BUILD:-0}" = "1" ]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier dev.local.financeapp.demo" "$CONTENTS/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName FinanceAppDemo" "$CONTENTS/Info.plist"
  /usr/libexec/PlistBuddy -c "Add :FinanceDemo bool true" "$CONTENTS/Info.plist"
fi
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

# Always build the frontend from current source, lockfile, config and public resources.
# Vite's own incremental work is cheap; a stale artifact is not a valid successful build.
log "building current frontend inputs"
( cd "$REPO_ROOT/frontend" && npx vite build --configLoader runner ) || die "frontend build failed"
cp -R "$REPO_ROOT/frontend/dist" "$CONTENTS/Resources/web"

# Distributable inputs must not identify the builder or include local financial data.
python3 "$MACOS_DIR/scripts/bundle_manifest.py" "$APP" "$REPO_ROOT" "$DEPENDENCY_FINGERPRINT" "$CONFIGURATION"

# --------------------------------------------------------------------- signing --
bash "$MACOS_DIR/scripts/sign.sh" "$APP" "${MACOS_SIGN_IDENTITY:--}"

log "built: $APP ($(du -sh "$APP" | cut -f1))"
log "run it with: open '$APP'"
