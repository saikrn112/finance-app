#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/_compose_runtime.sh"

GLOBAL_ENV_FILE="${HOME}/.config/finance-app/env"
if [ -f "${GLOBAL_ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${GLOBAL_ENV_FILE}"
  set +a
fi

export FINANCE_APP_GOOGLE_CLIENT_ID="${FINANCE_APP_GOOGLE_CLIENT_ID:-}"
export FINANCE_APP_GOOGLE_CLIENT_SECRET="${FINANCE_APP_GOOGLE_CLIENT_SECRET:-}"
export FINANCE_APP_GOOGLE_CLIENT_TYPE="${FINANCE_APP_GOOGLE_CLIENT_TYPE:-desktop}"

FORCE_RESTART=false
POSITIONAL_MODE="prod"
for arg in "$@"; do
  case "${arg}" in
    --restart) FORCE_RESTART=true ;;
    -*) ;;
    *) POSITIONAL_MODE="${arg}" ;;
  esac
done

MODE="$(resolve_mode "${POSITIONAL_MODE}")"

CANDIDATE_API_PORTS=(8000 8011 8021 8031 8041 8051 8061 8071 8081 8091)
CANDIDATE_WEB_PORTS=(5173 5181 5191 5201 5211 5221 5231 5241 5251 5261)

is_port_free() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    ! lsof -iTCP:"${port}" -sTCP:LISTEN -Pn >/dev/null 2>&1
    return
  fi
  python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

pick_port_pair() {
  local idx
  for idx in "${!CANDIDATE_API_PORTS[@]}"; do
    local api_port="${CANDIDATE_API_PORTS[$idx]}"
    local web_port="${CANDIDATE_WEB_PORTS[$idx]}"
    if is_port_free "${api_port}" && is_port_free "${web_port}"; then
      echo "${api_port} ${web_port}"
      return 0
    fi
  done
  return 1
}

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g; s/-\{2,\}/-/g; s/^-//; s/-$//'
}

PROJECT_NAME="$(slugify "$(basename "${REPO_ROOT}")")"

_list_containers() {
  local flag="${1:-}"
  if command -v docker >/dev/null 2>&1 && docker ps ${flag} --format '{{.Names}}' 2>/dev/null | grep -q "^${PROJECT_NAME}-"; then
    docker ps ${flag} --format '{{.Names}}' 2>/dev/null | grep "^${PROJECT_NAME}-"
    return 0
  fi
  if command -v finch >/dev/null 2>&1 && finch ps ${flag} --format '{{.Names}}' 2>/dev/null | grep -q "^${PROJECT_NAME}-"; then
    finch ps ${flag} --format '{{.Names}}' 2>/dev/null | grep "^${PROJECT_NAME}-"
    return 0
  fi
  return 1
}

start_prod() {
  local running stopped
  running="$(_list_containers | head -1 || true)"
  if [ -n "${running}" ]; then
    if [ "${FORCE_RESTART}" = true ]; then
      run_compose -p "${PROJECT_NAME}" down
    else
      echo "Stack '${PROJECT_NAME}' is already running (${running})."
      printf "Restart it? [y/N] "
      read -r answer
      if [[ "${answer}" =~ ^[Yy] ]]; then
        run_compose -p "${PROJECT_NAME}" down
      else
        echo "Aborted." >&2
        exit 0
      fi
    fi
  else
    stopped="$(_list_containers -a | head -1 || true)"
    if [ -n "${stopped}" ]; then
      run_compose -p "${PROJECT_NAME}" down 2>/dev/null
    fi
  fi

  read -r API_PORT WEB_PORT < <(pick_port_pair) || {
    echo "No free port pair found. Add more candidates in scripts/start.sh or stop an existing stack." >&2
    exit 1
  }

  DATA_DIR="${REPO_ROOT}/data"
  mkdir -p "${DATA_DIR}"

  export FINANCE_APP_HOST_API_PORT="${API_PORT}"
  export FINANCE_APP_PORT="${API_PORT}"
  export FINANCE_APP_HOST_WEB_PORT="${WEB_PORT}"
  export VITE_PORT="${WEB_PORT}"
  export VITE_API_PROXY_TARGET="http://localhost:${API_PORT}"
  export FINANCE_APP_HOST_DATA_DIR="${DATA_DIR}"
  export FINANCE_APP_DATA_DIR="/app/data"
  export FINANCE_APP_RUNTIME_DIR="/app/data/runtime/prod"
  export FINANCE_APP_DB_PATH="/app/data/runtime/prod/finances.db"
  export FINANCE_APP_GOOGLE_REDIRECT_URI="http://localhost:${API_PORT}/api/settings/vault/google/callback"

  COMPOSE_FILE="$(mktemp "${TMPDIR:-/tmp}/finances-start.XXXXXX.yml")"
  trap 'rm -f "${COMPOSE_FILE}"' EXIT
  cat > "${COMPOSE_FILE}" <<EOF
services:
  app:
    build:
      context: ${REPO_ROOT}
      dockerfile: Dockerfile
    working_dir: /app
    command: ["/bin/bash", "/app/scripts/start_from_docker.sh", "prod"]
    ports:
      - "${FINANCE_APP_HOST_API_PORT}:${FINANCE_APP_PORT}"
      - "${FINANCE_APP_HOST_WEB_PORT}:${VITE_PORT}"
    environment:
      PYTHONUNBUFFERED: "1"
      FINANCE_APP_PORT: "${FINANCE_APP_PORT}"
      FINANCE_APP_DATA_DIR: "${FINANCE_APP_DATA_DIR}"
      FINANCE_APP_RUNTIME_DIR: "${FINANCE_APP_RUNTIME_DIR}"
      FINANCE_APP_DB_PATH: "${FINANCE_APP_DB_PATH}"
      VITE_PORT: "${VITE_PORT}"
      VITE_API_PROXY_TARGET: "${VITE_API_PROXY_TARGET}"
      FINANCE_APP_GOOGLE_REDIRECT_URI: "${FINANCE_APP_GOOGLE_REDIRECT_URI}"
      FINANCE_APP_GOOGLE_CLIENT_ID: "${FINANCE_APP_GOOGLE_CLIENT_ID}"
      FINANCE_APP_GOOGLE_CLIENT_SECRET: "${FINANCE_APP_GOOGLE_CLIENT_SECRET}"
      FINANCE_APP_GOOGLE_CLIENT_TYPE: "${FINANCE_APP_GOOGLE_CLIENT_TYPE}"
      FINANCE_PLUGINS_DIR: "${FINANCE_PLUGINS_DIR:-}"
    volumes:
      - ${REPO_ROOT}:/app
      - ${FINANCE_APP_HOST_DATA_DIR}:/app/data
$([ -n "${FINANCE_PLUGINS_DIR}" ] && echo "      - ${FINANCE_PLUGINS_DIR}:${FINANCE_PLUGINS_DIR}:ro")
EOF

  echo "Starting finance app project '${PROJECT_NAME}' (prod)"
  echo "frontend: http://localhost:${WEB_PORT}"
  echo "api:      http://localhost:${API_PORT}"
  echo

  if run_compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d app; then
    cat <<EOF
Finance app is starting.

Open:
  http://localhost:${WEB_PORT}
EOF
    exit 0
  fi

  echo "Failed to start finance app." >&2
  exit 1
}

start_mode_service() {
  local service="$1"
  echo "Starting finance app project '${PROJECT_NAME}' (${MODE})"
  if run_compose -p "${PROJECT_NAME}" up -d "${service}"; then
    echo "Finance app stack is up (${MODE})."
    print_endpoints_for_mode "${MODE}"
    exit 0
  fi
  echo "Failed to start finance app (${MODE})." >&2
  exit 1
}

case "${MODE}" in
  prod) start_prod ;;
  demo) start_mode_service "app-demo" ;;
  replay) start_mode_service "app-replay" ;;
  all)
    echo "Unsupported mode for scripts/start.sh: all. Start prod, demo, or replay explicitly." >&2
    exit 1
    ;;
esac
