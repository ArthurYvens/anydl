"""The machine around the app: ffmpeg, Python, yt-dlp, the desktop."""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from importlib import metadata as importlib_metadata

from .config import APP_DIR, APP_NAME, BIN_DIR, YTDLP_PYPI_URL
from .text import parse_version


def fetch_thumbnail(url, ffmpeg_dir, width=280):
    """Save a thumbnail as PNG and return the path, or None if anything is off.

    Tk only reads PNG and GIF, and sites serve JPEG or WebP, so ffmpeg does the
    conversion. Without ffmpeg there is simply no picture.
    """
    if not url or not ffmpeg_dir:
        return None
    ffmpeg = os.path.join(ffmpeg_dir, "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(8 * 1024 * 1024)
        target = os.path.join(tempfile.gettempdir(), "anydl-thumb-%d.png" % os.getpid())
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", "pipe:0",
             "-vf", "scale=%d:-1" % width, "-frames:v", "1", target],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return target if result.returncode == 0 and os.path.isfile(target) else None
    except Exception:  # noqa: BLE001
        return None


def probe_height(path, ffmpeg_dir):
    """The vertical size of a file that exists, or None.

    The library index used to store the height of the format that was asked
    for, which is not the same thing. yt-dlp skips a download whose target
    name is already taken, so an accepted 1080p upgrade could leave the 720p
    file untouched and still be recorded as 1080p -- after which the upgrade
    was never offered again. Measuring the file cannot drift from it.
    """
    ffprobe = ffmpeg_tool("ffprobe", ffmpeg_dir)
    if not ffprobe or not path or not os.path.isfile(path):
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = result.stdout.decode("utf8", "replace").strip().splitlines()
        return int(text[0]) if text and text[0].strip().isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def ffmpeg_tool(name, ffmpeg_dir):
    """Path to ffmpeg, ffprobe or ffplay, or None when it is not there.

    The three ship together in every build anydl installs, but a system
    package can carry ffmpeg without ffplay, so each one is checked.
    """
    if not ffmpeg_dir:
        return None
    exe = os.path.join(ffmpeg_dir, name + (".exe" if sys.platform == "win32" else ""))
    return exe if os.path.isfile(exe) else None


def default_output_dir():
    """~/Downloads/anydl, falling back to ~/anydl."""
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    base = downloads if os.path.isdir(downloads) else home
    return os.path.join(base, APP_NAME)


def find_ffmpeg():
    """Look for ffmpeg on PATH, then in the bundled bin/ directory."""
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    if os.path.isdir(BIN_DIR):
        for root, _dirs, files in os.walk(BIN_DIR):
            for name in files:
                if name.lower() in ("ffmpeg.exe", "ffmpeg"):
                    return root
    return None


def python_executable():
    """The interpreter to run pip with: python.exe, never pythonw.exe."""
    executable = sys.executable
    if sys.platform == "win32" and os.path.basename(executable).lower() == "pythonw.exe":
        sibling = os.path.join(os.path.dirname(executable), "python.exe")
        if os.path.isfile(sibling):
            return sibling
    return executable


def installed_ytdlp_version(prefer_loaded=True):
    """The yt-dlp version on this machine, or None if it is not installed.

    Read from the package metadata rather than by importing yt_dlp: an import
    pins the old code in memory for the life of the process, and an update the
    user just clicked should take effect without restarting the app. Once a
    download has loaded the module anyway, that is the honest answer -- except
    for the caller checking what pip just wrote, hence prefer_loaded=False.
    """
    if prefer_loaded and "yt_dlp" in sys.modules:
        submodule = getattr(sys.modules["yt_dlp"], "version", None)
        loaded = getattr(submodule, "__version__", None)
        if loaded:
            return loaded
    try:
        importlib.invalidate_caches()  # or an upgrade stays invisible until restart
        return importlib_metadata.version("yt-dlp")
    except Exception:  # noqa: BLE001
        return None


def latest_ytdlp_version(timeout=8):
    """The newest release on PyPI. Raises if the network is not there."""
    request = urllib.request.Request(YTDLP_PYPI_URL, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["info"]["version"]


def _run_pip(command, on_log):
    """Run pip, streaming its output to on_log. Returns (ok, combined output)."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Without this a console window flashes up when the app was started
            # from pythonw.exe.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return False, str(exc)

    lines = []
    for line in process.stdout:
        line = line.rstrip()
        if line:
            lines.append(line)
            on_log("[pip] " + line)
    process.wait()
    return process.returncode == 0, "\n".join(lines)


def update_ytdlp(on_log=print):
    """pip install -U yt-dlp. Returns (ok, message) for the caller to show."""
    before = installed_ytdlp_version(prefer_loaded=False)
    command = [python_executable(), "-m", "pip", "install", "-U",
               "--disable-pip-version-check", "yt-dlp"]

    ok, output = _run_pip(command, on_log)
    if not ok and "permission" in output.lower() and sys.prefix == sys.base_prefix:
        # A Python installed for all users will not let a normal account write
        # into site-packages. The user site directory always works.
        on_log("[pip] no write access there; retrying into the user directory")
        ok, output = _run_pip(command + ["--user"], on_log)

    if not ok:
        # Debian, Ubuntu and Fedora refuse pip installs into the system Python,
        # and a server image often has no pip at all. Telling either to run pip
        # by hand is advice that cannot work.
        if "externally-managed-environment" in output or "No module named pip" in output:
            return False, ("This Python cannot install packages: it belongs to the "
                           "distribution. Run ./install.sh, which keeps yt-dlp in a "
                           "local .venv, and start anydl with ./run.sh.")
        return False, ("Could not update yt-dlp. Do it by hand with:\n"
                       "    python -m pip install -U yt-dlp")

    after = installed_ytdlp_version(prefer_loaded=False)
    if after and before and parse_version(after) == parse_version(before):
        return True, "yt-dlp was already up to date (%s)." % after
    if "yt_dlp" in sys.modules:
        return True, "yt-dlp updated to %s. Restart anydl to use it." % (after or "?")
    return True, "yt-dlp updated to %s." % (after or "?")


def open_in_file_manager(path):
    """Reveal a directory in the platform's file manager.

    Returns a message rather than raising: a bare desktop may have no xdg-open,
    and an exception inside a Tk callback goes nowhere the user can see.
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        return "Could not open %s (%s)" % (path, exc)
    return None


def claim_taskbar_identity():
    """Give Windows an app id of our own before any window exists.

    The taskbar groups buttons by AppUserModelID and takes the icon from that
    group. Launched through pythonw.exe we inherit Python's id, so the button
    shows the generic Python icon no matter what the window icon says.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "anydl.downloader")
    except Exception:  # noqa: BLE001
        pass


def apply_window_icon(window):
    """Set the window icon, quietly giving up if anything is off.

    Windows wants the .ico; everywhere else Tk reads the PNG. A missing or
    unreadable icon must never be the reason the app fails to open.
    """
    import tkinter as tk

    ico = os.path.join(APP_DIR, "assets", APP_NAME + ".ico")
    png = os.path.join(APP_DIR, "assets", APP_NAME + ".png")
    try:
        if sys.platform == "win32" and os.path.isfile(ico):
            # default= also covers the message boxes this app opens later.
            window.iconbitmap(default=ico)
            return
        if os.path.isfile(png):
            # Tk drops the image if nothing keeps a reference to it.
            window._icon = tk.PhotoImage(file=png)
            window.iconphoto(True, window._icon)
    except Exception:  # noqa: BLE001
        pass
