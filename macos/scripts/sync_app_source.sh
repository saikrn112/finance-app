#!/usr/bin/env bash
# Copy the app's own Python into an existing backend payload.
#
# Usage: sync_app_source.sh [backend-dir]
#
# Split out of build_backend.sh so it can run on *every* build. The payload's expensive half --
# the relocatable interpreter and the wheelhouse -- is cached and rarely changes; the app's own
# source changes constantly. Bundling them into one cached step meant a normal
# `build_app.sh` shipped stale Python: a new route existed in the repo, the app 404ed for it,
# and nothing said why. `FORCE_BACKEND=1` was the workaround, which is another way of saying the
# default was wrong.
#
# Cheap enough to be unconditional: a few hundred kilobytes of .py files.

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BACKEND_DIR="${1:-$BUILD_DIR/backend}"
[ -d "$BACKEND_DIR" ] || die "no backend payload at $BACKEND_DIR -- run build_backend.sh first"

APP_DIR="$BACKEND_DIR/app"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"

for item in src rules; do
  cp -R "$REPO_ROOT/$item" "$APP_DIR/$item"
done

# The tracked example, so src/config.py has defaults to fall back on when the user has no
# config.yaml yet. Only the *example* ships -- config.yaml itself holds real Plaid and Google
# secrets and is gitignored.
cp "$REPO_ROOT/config.yaml.example" "$APP_DIR/config.yaml.example"

mkdir -p "$APP_DIR/plugins"
# Tracked template plugins only -- never whatever happens to be sitting in the working tree,
# which on a real machine is the owner's private parsers.
( cd "$REPO_ROOT" && git ls-files plugins ) | while read -r tracked; do
  mkdir -p "$APP_DIR/$(dirname "$tracked")"
  cp "$REPO_ROOT/$tracked" "$APP_DIR/$tracked"
done

find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$APP_DIR" -name '*.pyc' -delete

cp "$MACOS_DIR/payload/bootstrap.py" "$BACKEND_DIR/bootstrap.py"
cp "$MACOS_DIR/payload/import_smoke.py" "$BACKEND_DIR/import_smoke.py"

log "synced application source into $(basename "$BACKEND_DIR")/app"
