#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== Finance App: Local Install ==="
echo "Repo: ${REPO_ROOT}"
echo

# Python
echo "[1/4] Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.11+." >&2
  exit 1
fi
py_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Found python3 ${py_version}"

# Node
echo "[2/4] Checking Node.js..."
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node not found. Install Node.js 18+." >&2
  exit 1
fi
echo "  Found node $(node --version)"

# Python deps
echo "[3/4] Installing Python dependencies..."
cd "${REPO_ROOT}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  echo "  Created .venv"
fi
source .venv/bin/activate
pip install --quiet -e .
echo "  Python deps installed"

# Frontend deps
echo "[4/4] Installing frontend dependencies..."
cd "${REPO_ROOT}/frontend"
npm install --silent
echo "  Frontend deps installed"

echo
echo "=== Install complete ==="
echo "Run: bash scripts/start_local.sh"
