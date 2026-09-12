#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

# Activate venv
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
  source venv/bin/activate
else
  echo "ERROR: No virtualenv found. Run: bash scripts/install.sh" >&2
  exit 1
fi

# Load global env if present
GLOBAL_ENV_FILE="${HOME}/.config/finance-app/env"
if [ -f "${GLOBAL_ENV_FILE}" ]; then
  set -a
  source "${GLOBAL_ENV_FILE}"
  set +a
fi

# Parse args (same interface as start.sh)
FORCE_RESTART=false
POSITIONAL_MODE="prod"
for arg in "$@"; do
  case "${arg}" in
    --restart) FORCE_RESTART=true ;;
    -*) ;;
    *) POSITIONAL_MODE="${arg}" ;;
  esac
done

MODE="${POSITIONAL_MODE}"

# Defaults
export FINANCE_PLUGINS_DIR="${FINANCE_PLUGINS_DIR:-}"
export FINANCE_APP_GOOGLE_CLIENT_ID="${FINANCE_APP_GOOGLE_CLIENT_ID:-}"
export FINANCE_APP_GOOGLE_CLIENT_SECRET="${FINANCE_APP_GOOGLE_CLIENT_SECRET:-}"
export FINANCE_APP_GOOGLE_CLIENT_TYPE="${FINANCE_APP_GOOGLE_CLIENT_TYPE:-desktop}"

# Port candidates (same as start.sh)
CANDIDATE_API_PORTS=(8000 8011 8021 8031 8041 8051 8061 8071 8081 8091)
CANDIDATE_WEB_PORTS=(5173 5181 5191 5201 5211 5221 5231 5241 5251 5261)

is_port_free() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

pick_port_pair() {
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

# PID file for restart detection
PID_FILE="${REPO_ROOT}/data/runtime/${MODE}/.local_pids"

_running_pids() {
  if [ -f "${PID_FILE}" ]; then
    local backend_pid frontend_pid
    read -r backend_pid frontend_pid < "${PID_FILE}" 2>/dev/null || true
    if [ -n "${backend_pid}" ] && kill -0 "${backend_pid}" 2>/dev/null; then
      echo "${backend_pid} ${frontend_pid}"
      return 0
    fi
  fi
  return 1
}

_stop_existing() {
  local pids
  if pids=$(_running_pids); then
    local backend_pid frontend_pid
    read -r backend_pid frontend_pid <<< "${pids}"
    kill "${backend_pid}" "${frontend_pid}" 2>/dev/null || true
    wait "${backend_pid}" "${frontend_pid}" 2>/dev/null || true
    rm -f "${PID_FILE}"
    echo "Stopped existing instance."
  fi
}

# Check for existing instance
if _running_pids >/dev/null 2>&1; then
  if [ "${FORCE_RESTART}" = true ]; then
    _stop_existing
  else
    echo "Finance app (local, ${MODE}) is already running."
    printf "Restart it? [y/N] "
    read -r answer
    if [[ "${answer}" =~ ^[Yy] ]]; then
      _stop_existing
    else
      echo "Aborted." >&2
      exit 0
    fi
  fi
fi

# Pick free ports
read -r API_PORT WEB_PORT < <(pick_port_pair) || {
  echo "No free port pair found." >&2
  exit 1
}

# Mode-specific config
case "${MODE}" in
  prod)
    export FINANCE_APP_DATA_DIR="data"
    export FINANCE_APP_RUNTIME_DIR="data/runtime/prod"
    export FINANCE_APP_DB_PATH="data/runtime/prod/finances.db"
    backend_cmd="serve"
    ;;
  demo)
    export FINANCE_APP_DATA_DIR="data"
    export FINANCE_APP_RUNTIME_DIR="data/runtime/demo"
    export FINANCE_APP_DB_PATH="data/runtime/demo/finances.db"
    backend_cmd="demo"
    ;;
  replay)
    export FINANCE_APP_DATA_DIR="data"
    export FINANCE_APP_RUNTIME_DIR="data/runtime/replay"
    export FINANCE_APP_DB_PATH="data/runtime/replay/finances.db"
    backend_cmd="serve"
    ;;
  *)
    echo "Unknown mode: ${MODE}. Use prod, demo, or replay." >&2
    exit 1
    ;;
esac

export FINANCE_APP_PORT="${API_PORT}"
export VITE_PORT="${WEB_PORT}"
export VITE_API_PROXY_TARGET="http://127.0.0.1:${API_PORT}"
# Must be the API port, not the web port: this exact URI has to be registered in the
# Google Cloud console, and the callback is served by the backend. Pointing it at the
# Vite port makes Google reject the sign-in with redirect_uri_mismatch.
export FINANCE_APP_GOOGLE_REDIRECT_URI="http://localhost:${API_PORT}/api/settings/vault/google/callback"

mkdir -p "${FINANCE_APP_RUNTIME_DIR}"

echo "Starting finance app (local, ${MODE})"
echo "  Backend:  http://localhost:${API_PORT}"
echo "  Frontend: http://localhost:${WEB_PORT}"
echo "  Plugins:  ${FINANCE_PLUGINS_DIR:-<none>}"
echo

# Cleanup on exit
cleanup() {
  echo
  echo "Shutting down..."
  kill 0 2>/dev/null || true
  rm -f "${PID_FILE}"
}

trap cleanup EXIT INT TERM

# Start backend
python -m src.main "${backend_cmd}" --host 127.0.0.1 --port "${API_PORT}" --no-reload &
backend_pid=$!

# Wait for backend to be ready
echo -n "Waiting for backend..."
for i in $(seq 1 30); do
  if curl -s "http://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1; then
    echo " ready"
    break
  fi
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    echo " FAILED (backend crashed)"
    exit 1
  fi
  sleep 1
done

# Start frontend
cd "${REPO_ROOT}/frontend"
node_modules/.bin/vite --host 127.0.0.1 --port "${WEB_PORT}" &
frontend_pid=$!
cd "${REPO_ROOT}"

# Save PIDs
echo "${backend_pid} ${frontend_pid}" > "${PID_FILE}"

echo "Press Ctrl+C to stop"
wait
