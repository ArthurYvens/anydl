#!/usr/bin/env bash
set -euo pipefail

python -m compileall .
python -m pytest -q

if python -c "import ruff" >/dev/null 2>&1; then
  python -m ruff check .
elif command -v ruff >/dev/null 2>&1; then
  ruff check .
else
  echo "Ruff not installed; skipping lint."
fi
