#!/usr/bin/env bash
# Start Ledgerline. Creates the virtualenv and installs dependencies if needed.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"

if [ ! -x .venv/bin/python ]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  uv venv .venv
  uv pip install --python .venv/bin/python -r requirements.txt
fi

exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
