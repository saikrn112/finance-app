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
# Build the frontend when the sources are newer than the last build.
#
# This script used to *copy* frontend/dist and never build it, which meant a bundle could ship a
# frontend days older than the source with nothing complaining. It happened: a fix to the getting
# started tour was edited, "verified" by grepping the bundle for a string that was already there, and
# shipped four times without ever reaching the app. A build script that silently packages stale
# artifacts is worse than one that fails.
if [ -d "$REPO_ROOT/frontend/src" ]; then
  newest_source="$(find "$REPO_ROOT/frontend/src" -type f -newer "$REPO_ROOT/frontend/dist/index.html" -print -quit 2>/dev/null || true)"
  if [ ! -f "$REPO_ROOT/frontend/dist/index.html" ] || [ -n "$newest_source" ]; then
    log "frontend/dist is stale (or missing) -- running the frontend build"
    # `vite build`, not `npm run build`. The npm script is `tsc -b && vite build`, and this repo
    # carries ~57 pre-existing type errors, so that script always fails -- which is exactly why
    # frontend/dist sat four days stale while the bundle happily shipped it. Vite transpiles without
    # type-checking, so it produces a correct bundle regardless.
    #
    # Types are still checked, just not here: `npx tsc -p tsconfig.app.json --noEmit`, compared
    # against the recorded baseline (see AGENTS.md). Coupling the shippable artifact to a
    # long-standing type debt only guaranteed a stale artifact.
    ( cd "$REPO_ROOT/frontend" && npx vite build ) \
      || die "the frontend build failed; refusing to ship a stale web UI"
  else
    log "frontend/dist is up to date"
  fi
fi

if [ -d "$REPO_ROOT/frontend/dist" ]; then
  rm -rf "$CONTENTS/Resources/web"
  cp -R "$REPO_ROOT/frontend/dist" "$CONTENTS/Resources/web"
  log "included frontend/dist as Resources/web"
else
  warn "no frontend/dist -- bundle has no web UI yet (phase 2)"
fi

# --------------------------------------------------------------------- signing --
bash "$MACOS_DIR/scripts/sign.sh" "$APP" "${MACOS_SIGN_IDENTITY:--}"

log "built: $APP ($(du -sh "$APP" | cut -f1))"
log "run it with: open '$APP'"
