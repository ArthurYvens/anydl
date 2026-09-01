#!/usr/bin/env bash
# YTConverter setup for Linux and macOS.
# Installs yt-dlp into a local virtual environment and checks for ffmpeg.
set -euo pipefail

cd "$(dirname "$0")"

echo "=========================================================="
echo "   YTConverter - setup"
echo "=========================================================="
echo

# ------------------------------------------------------------------- Python
echo "[1/3] Looking for Python 3..."
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "  [ERROR] Python 3.9+ not found. Install it with your package manager:"
    echo "          Debian/Ubuntu : sudo apt install python3 python3-venv python3-tk"
    echo "          Fedora        : sudo dnf install python3 python3-tkinter"
    echo "          Arch          : sudo pacman -S python tk"
    echo "          macOS         : brew install python-tk"
    exit 1
fi
echo "      Found: $($PY -c 'import sys; print(sys.executable)')"

if ! "$PY" -c 'import tkinter' 2>/dev/null; then
    echo "      [WARNING] tkinter is missing, so the graphical interface will not open."
    echo "                The command line mode still works. Install the tk package"
    echo "                for your distro (python3-tk, python3-tkinter or tk)."
fi
echo

# ------------------------------------------------------------------- yt-dlp
echo "[2/3] Installing yt-dlp into .venv ..."
# Many distros ship an externally managed Python, where a plain `pip install`
# is refused. A local venv sidesteps that and keeps the system untouched.
if [ ! -d .venv ]; then
    "$PY" -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade --quiet pip
./.venv/bin/python -m pip install --upgrade --quiet yt-dlp
echo "      yt-dlp $(./.venv/bin/python -c 'import yt_dlp; print(yt_dlp.version.__version__)') ready."
echo

# ------------------------------------------------------------------- ffmpeg
echo "[3/3] Looking for ffmpeg..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "      Found: $(command -v ffmpeg)"
else
    echo "      Not found. Without it the app is capped at ~720p and cannot make MP3s."
    echo "      Install it with:"
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "          brew install ffmpeg"
    else
        echo "          Debian/Ubuntu : sudo apt install ffmpeg"
        echo "          Fedora        : sudo dnf install ffmpeg"
        echo "          Arch          : sudo pacman -S ffmpeg"
    fi
fi

echo
echo "=========================================================="
echo "   All set. Run ./run.sh to start the app."
echo "=========================================================="
