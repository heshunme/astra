#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN_DEFAULT="$ROOT_DIR/.venv/bin/python"
PYTHON_BIN="${ASTRA_SMOKE_PYTHON:-$PYTHON_BIN_DEFAULT}"
SCRIPT_PATH="$ROOT_DIR/scripts/smoke_cli.py"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python interpreter not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_PATH" "$@"
