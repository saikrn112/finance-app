#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/_compose_runtime.sh"

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/-\{2,\}/-/g; s/^-//; s/-$//'
}

PROJECT_NAME="$(slugify "$(basename "${REPO_ROOT}")")"

echo "Finance app project '${PROJECT_NAME}'"

if run_compose -p "${PROJECT_NAME}" ps; then
  exit 0
fi

echo "Failed to inspect project '${PROJECT_NAME}'." >&2
exit 1
