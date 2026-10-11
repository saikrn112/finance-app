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

# Use Git's tracked allowlist for every source/resource tree, not only plugins.
# Local databases, private rules, scratch fixtures and ignored files cannot enter the payload.
( cd "$REPO_ROOT" && git ls-files -z -- src rules plugins config.yaml.example ) | \
  while IFS= read -r -d '' tracked; do
    [ -f "$REPO_ROOT/$tracked" ] || die "tracked input missing: $tracked"
    [ ! -L "$REPO_ROOT/$tracked" ] || die "symlink input refused: $tracked"
    case "$tracked" in
      */__pycache__/*|*.pyc|*.db|*.sqlite*|*/config.yaml|*/.env) die "private or generated input refused: $tracked" ;;
    esac
    mkdir -p "$APP_DIR/$(dirname "$tracked")"
    cp "$REPO_ROOT/$tracked" "$APP_DIR/$tracked"
  done

find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$APP_DIR" -name '*.pyc' -delete

cp "$MACOS_DIR/payload/bootstrap.py" "$BACKEND_DIR/bootstrap.py"
cp "$MACOS_DIR/payload/import_smoke.py" "$BACKEND_DIR/import_smoke.py"

log "synced application source into $(basename "$BACKEND_DIR")/app"
