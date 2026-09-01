#!/usr/bin/env bash
# Starts YTConverter. Any arguments are forwarded to the CLI; with none, the
# graphical interface opens.
set -euo pipefail

cd "$(dirname "$0")"

if [ -x ./.venv/bin/python ]; then
    PY=./.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi

if ! "$PY" -c 'import yt_dlp' 2>/dev/null; then
    echo "yt-dlp not installed. Running the setup..."
    ./install.sh
    PY=./.venv/bin/python
fi

exec "$PY" ytconverter.py "$@"
