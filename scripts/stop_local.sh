#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${1:-all}"

_stop_mode() {
  local mode="$1"
  local pid_file="${REPO_ROOT}/data/runtime/${mode}/.local_pids"
  if [ ! -f "${pid_file}" ]; then
    echo "No running instance found for ${mode}."
    return 1
  fi
  local backend_pid frontend_pid
  read -r backend_pid frontend_pid < "${pid_file}" 2>/dev/null || true
  local stopped=false
  if [ -n "${backend_pid}" ] && kill -0 "${backend_pid}" 2>/dev/null; then
    kill "${backend_pid}" 2>/dev/null || true
    stopped=true
  fi
  if [ -n "${frontend_pid}" ] && kill -0 "${frontend_pid}" 2>/dev/null; then
    kill "${frontend_pid}" 2>/dev/null || true
    stopped=true
  fi
  rm -f "${pid_file}"
  if [ "${stopped}" = true ]; then
    echo "Stopped ${mode}."
  else
    echo "No running processes for ${mode} (stale pid file removed)."
  fi
}

case "${MODE}" in
  all)
    for m in prod demo replay; do
      _stop_mode "${m}" 2>/dev/null || true
    done
    ;;
  prod|demo|replay)
    _stop_mode "${MODE}"
    ;;
  *)
    echo "Unknown mode: ${MODE}. Use all, prod, demo, or replay." >&2
    exit 1
    ;;
esac
