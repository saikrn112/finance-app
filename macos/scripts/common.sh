#!/usr/bin/env bash
# Shared settings for the macOS bundling scripts.
#
# Sourced, not executed. Keep bash-3.2 compatible: macOS ships bash 3.2 and an
# empty array under `set -u` is an unbound variable there, so any array
# expansion must use the ${arr[@]+"${arr[@]}"} form.

set -euo pipefail

MACOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$MACOS_DIR/.." && pwd)"
BUILD_DIR="$MACOS_DIR/build"
CACHE_DIR="$BUILD_DIR/cache"

APP_NAME="FinanceApp"
BUNDLE_ID="dev.local.financeapp"

# Arm64-only for now. Intel support means universal2 wheels for every native
# package (or a second architecture slice) -- see plan §3 decision 4.
TARGET_ARCH="arm64"
PY_MM="3.12"
PY_FULL="3.12.14"
PBS_TAG="20260901"
PBS_ASSET="cpython-${PY_FULL}+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_FULL}%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"

# The bundle's Python dependency set.
#
# Differences from pyproject.toml, both deliberate (plan §3):
#   * plain `uvicorn`, not `uvicorn[standard]` -- uvloop/httptools/watchfiles are
#     three extra compiled wheels and three extra signing surfaces, bought for a
#     performance win that is irrelevant for one local user on SQLite.
#   * no `google-generativeai` -- it drags in grpcio, the single most likely
#     cause of a notarization/hardened-runtime failure. The categorizer already
#     imports it lazily (src/processing/categorizer.py), so the app degrades to
#     "AI categorisation unavailable in this build" instead of failing.
BUNDLE_REQUIREMENTS="fastapi uvicorn sqlalchemy plaid-python pydantic pydantic-settings python-multipart pdfplumber pyyaml click"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
