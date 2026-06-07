#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/_compose_runtime.sh"

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/-\{2,\}/-/g; s/^-//; s/-$//'
}

PROJECT_NAME="$(slugify "$(basename "${REPO_ROOT}")")"
MODE="${1:-all}"

echo "Stopping finance app project '${PROJECT_NAME}' (${MODE})"

case "${MODE}" in
  all)
    if run_compose -p "${PROJECT_NAME}" down; then
      echo "Stopped project '${PROJECT_NAME}'."
      exit 0
    fi
    ;;
  prod)
    if run_compose -p "${PROJECT_NAME}" stop app && run_compose -p "${PROJECT_NAME}" rm -f app; then
      echo "Stopped prod for '${PROJECT_NAME}'."
      exit 0
    fi
    ;;
  demo)
    if run_compose -p "${PROJECT_NAME}" stop app-demo && run_compose -p "${PROJECT_NAME}" rm -f app-demo; then
      echo "Stopped demo for '${PROJECT_NAME}'."
      exit 0
    fi
    ;;
  replay)
    if run_compose -p "${PROJECT_NAME}" stop app-replay && run_compose -p "${PROJECT_NAME}" rm -f app-replay; then
      echo "Stopped replay for '${PROJECT_NAME}'."
      exit 0
    fi
    ;;
  *)
    echo "Unknown mode: ${MODE}. Use all, prod, demo, or replay." >&2
    exit 1
    ;;
esac

echo "Failed to stop project '${PROJECT_NAME}' (${MODE})." >&2
exit 1
