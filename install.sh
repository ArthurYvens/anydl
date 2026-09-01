#!/usr/bin/env bash
# anydl setup for Linux and macOS.
# Installs yt-dlp into a local virtual environment and checks for ffmpeg.
set -euo pipefail

cd "$(dirname "$0")"

# Colour only when stdout is a terminal and the user has not opted out.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_TITLE=$'\033[1;96m'; C_LINE=$'\033[38;5;99m'; C_STEP=$'\033[96m'
    C_OK=$'\033[92m';      C_WARN=$'\033[93m';      C_FAIL=$'\033[91m'
    C_DIM=$'\033[90m';     C_OFF=$'\033[0m'
else
    C_TITLE=''; C_LINE=''; C_STEP=''; C_OK=''
    C_WARN='';  C_FAIL='';  C_DIM=''; C_OFF=''
fi

step() { printf '  %s[%s/3]%s %s%s%s\n' "$C_STEP" "$1" "$C_OFF" "$C_TITLE" "$2" "$C_OFF"; }
ok()   { printf '        %sok%s   %s\n' "$C_OK" "$C_OFF" "$1"; }
info() { printf '        %s..%s   %s%s%s\n' "$C_DIM" "$C_OFF" "$C_DIM" "$1" "$C_OFF"; }
warn() { printf '        %s!!%s   %s\n' "$C_WARN" "$C_OFF" "$1"; }
fail() { printf '        %sxx%s   %s\n' "$C_FAIL" "$C_OFF" "$1"; }
hint() { printf '             %s%s%s\n' "$C_DIM" "$1" "$C_OFF"; }

printf '\n'
printf '  %s============================================%s\n' "$C_LINE" "$C_OFF"
printf '  %s|%s  %sanydl%s                                   %s|%s\n' \
       "$C_LINE" "$C_OFF" "$C_TITLE" "$C_OFF" "$C_LINE" "$C_OFF"
printf '  %s|%s  %svideo and audio downloader%s              %s|%s\n' \
       "$C_LINE" "$C_OFF" "$C_DIM" "$C_OFF" "$C_LINE" "$C_OFF"
printf '  %s============================================%s\n\n' "$C_LINE" "$C_OFF"

# ------------------------------------------------------------------- Python
step 1 "Python"
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    fail "Python 3.9+ not found"
    hint "Debian/Ubuntu : sudo apt install python3 python3-venv python3-tk"
    hint "Fedora        : sudo dnf install python3 python3-tkinter"
    hint "Arch          : sudo pacman -S python tk"
    hint "macOS         : brew install python-tk"
    printf '\n'
    exit 1
fi
ok "found  $("$PY" -c 'import sys; print(sys.executable)')"

if ! "$PY" -c 'import tkinter' 2>/dev/null; then
    warn "tkinter missing, so the window will not open"
    hint "the command line still works; install python3-tk, python3-tkinter or tk"
fi
printf '\n'

# ------------------------------------------------------------------- yt-dlp
step 2 "yt-dlp"
# Many distros ship an externally managed Python where a plain `pip install`
# is refused. A local venv sidesteps that and leaves the system untouched.
if [ ! -d .venv ]; then
    info "creating .venv"
    "$PY" -m venv .venv
fi
info "installing with pip"
./.venv/bin/python -m pip install --upgrade --quiet pip
./.venv/bin/python -m pip install --upgrade --quiet yt-dlp
ok "yt-dlp $(./.venv/bin/python -c 'import yt_dlp; print(yt_dlp.version.__version__)')"
printf '\n'

# ------------------------------------------------------------------- ffmpeg
step 3 "ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
    ok "found  $(command -v ffmpeg)"
else
    warn "not installed"
    hint "without it: capped at ~720p, and no MP3 output"
    info "install it yourself, since the package manager needs your password:"
    if [ "$(uname -s)" = "Darwin" ]; then
        hint "brew install ffmpeg"
    else
        hint "Debian/Ubuntu : sudo apt install ffmpeg"
        hint "Fedora        : sudo dnf install ffmpeg"
        hint "Arch          : sudo pacman -S ffmpeg"
    fi
fi

printf '\n'
printf '  %s============================================%s\n' "$C_OK" "$C_OFF"
printf '   %sReady.%s  Run %s./run.sh%s to start anydl.\n' \
       "$C_OK" "$C_OFF" "$C_TITLE" "$C_OFF"
printf '  %s============================================%s\n\n' "$C_OK" "$C_OFF"
