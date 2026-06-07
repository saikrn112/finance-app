#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-prod}"

cd /app

if [ ! -d frontend/node_modules ]; then
  cd frontend
  npm install
  cd /app
fi

backend_port="${FINANCE_APP_PORT:-8000}"
frontend_port="${VITE_PORT:-5173}"
backend_cmd="serve"
frontend_cmd=(npm run dev -- --host 0.0.0.0 --port "${frontend_port}")

case "${MODE}" in
  prod)
    ;;
  demo)
    backend_cmd="demo"
    backend_port="${FINANCE_APP_PORT:-8001}"
    frontend_cmd=(npm run dev:demo -- --host 0.0.0.0)
    ;;
  replay)
    backend_port="${FINANCE_APP_PORT:-8002}"
    frontend_port="${VITE_PORT:-5175}"
    frontend_cmd=(npm run dev -- --host 0.0.0.0 --port "${frontend_port}")
    ;;
  *)
    echo "Unknown docker start mode: ${MODE}. Use prod, demo, or replay." >&2
    exit 1
    ;;
esac

python -m src.main "${backend_cmd}" --host 0.0.0.0 --port "${backend_port}" --no-reload &
backend_pid=$!

(
  cd /app/frontend
  exec "${frontend_cmd[@]}"
) &
frontend_pid=$!

cleanup() {
  kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

wait -n "$backend_pid" "$frontend_pid"
