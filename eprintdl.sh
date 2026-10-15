#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

if command -v eprintdl >/dev/null 2>&1; then
  exec eprintdl "$@"
fi

PYTHON_BIN=""
for candidate in "${SCRIPT_DIR}/.venv/bin/python" python3 python; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    PYTHON_BIN="${candidate}"
    break
  fi
done

if [ -z "${PYTHON_BIN}" ]; then
  echo "eprintdl: python3 not found. Install Python 3.10 or newer." >&2
  exit 2
fi

export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON_BIN}" -X utf8 -m eprintdl "$@"
