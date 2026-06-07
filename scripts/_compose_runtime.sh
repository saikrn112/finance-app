#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

resolve_mode() {
  local mode="${1:-prod}"
  case "${mode}" in
    prod|demo|replay|all)
      printf '%s\n' "${mode}"
      ;;
    *)
      echo "Unknown mode: ${mode}. Use prod, demo, replay, or all." >&2
      return 1
      ;;
  esac
}

compose_service_for_mode() {
  local mode
  mode="$(resolve_mode "${1:-prod}")" || return 1
  case "${mode}" in
    prod) printf '%s\n' "app" ;;
    demo) printf '%s\n' "app-demo" ;;
    replay) printf '%s\n' "app-replay" ;;
    all) printf '%s\n' "app app-demo app-replay" ;;
  esac
}

ensure_finch_vm() {
  local status
  status="$(finch vm status 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  if [ "${status}" = "running" ]; then
    return 0
  fi

  if [ "${status}" = "nonexistent" ] || [ -z "${status}" ]; then
    echo "Preparing Finch VM..." >&2
    finch vm init >/dev/null 2>&1 || true
  fi

  echo "Starting Finch VM..." >&2
  if finch vm start >/dev/null 2>&1; then
    return 0
  fi

  # One more init/start pass for machines where init is required before start.
  echo "Retrying Finch VM initialization..." >&2
  if [ "${status}" != "running" ]; then
    finch vm init >/dev/null 2>&1 || true
  fi
  finch vm start >/dev/null 2>&1
}

run_with_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    return 1
  fi
  (
    cd "${REPO_ROOT}"
    docker compose "$@"
  )
}

run_with_finch() {
  if ! command -v finch >/dev/null 2>&1; then
    return 1
  fi
  ensure_finch_vm
  local attempt=1
  local max_attempts=3
  while [ "${attempt}" -le "${max_attempts}" ]; do
    if (
      cd "${REPO_ROOT}"
      finch compose "$@"
    ); then
      return 0
    fi
    if [ "${attempt}" -lt "${max_attempts}" ]; then
      echo "Finch compose attempt ${attempt} failed. Retrying..." >&2
      sleep 2
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

run_compose() {
  if run_with_docker "$@"; then
    return 0
  fi
  echo "Docker compose failed or is unavailable. Falling back to Finch..." >&2
  if run_with_finch "$@"; then
    return 0
  fi
  echo "No supported container runtime found or both Docker and Finch failed." >&2
  return 1
}

print_endpoints() {
  cat <<'EOF'
prod:   http://localhost:5173  api: http://localhost:8000
demo:   http://localhost:5174  api: http://localhost:8001
replay: http://localhost:5175  api: http://localhost:8002
EOF
}

print_endpoints_for_mode() {
  local mode
  mode="$(resolve_mode "${1:-prod}")" || return 1
  case "${mode}" in
    prod)
      echo "prod:   http://localhost:5173  api: http://localhost:8000"
      ;;
    demo)
      echo "demo:   http://localhost:5174  api: http://localhost:8001"
      ;;
    replay)
      echo "replay: http://localhost:5175  api: http://localhost:8002"
      ;;
    all)
      print_endpoints
      ;;
  esac
}
