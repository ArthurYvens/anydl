"""
anydl - download video as MP4 from ~1700 sites, or extract just the audio
(MP3, M4A, WAV, OPUS, FLAC). Anything yt-dlp supports: YouTube, Instagram,
X/Twitter, TikTok, Vimeo, SoundCloud, Twitch and the rest.

    python anydl.py                 open the graphical interface
    python anydl.py URL --audio     command line mode

Requires yt-dlp. ffmpeg is optional but strongly recommended: without it the
app falls back to single-stream video (~720p) and cannot transcode audio.
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import argparse
import importlib
import threading
import subprocess
import urllib.request
from importlib import metadata as importlib_metadata

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(APP_DIR, "bin")

APP_NAME = "anydl"
VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
AUDIO_FORMATS = ["mp3", "m4a", "wav", "opus", "flac"]
AUDIO_BITRATES = ["320", "256", "192", "160", "128", "96"]

# One click instead of three dropdowns, for the jobs people actually come here
# to do. A preset is nothing but a set of the same choices made below, so the
# dropdowns keep showing what it picked and stay editable afterwards.
PRESETS = [
    {"name": "Music 320", "cli": "music",
     "settings": {"mode": "audio", "audio_format": "mp3", "bitrate": "320"}},
    {"name": "Archive 1080p", "cli": "archive",
     "settings": {"mode": "video", "quality": "1080p"}},
    {"name": "Phone 720p", "cli": "phone",
     "settings": {"mode": "video", "quality": "720p"}},
]
CUSTOM_PRESET = "Custom"

# Sites like Instagram, Vimeo or a private playlist only answer to a logged-in
# session. yt-dlp can borrow one from a local browser profile.
BROWSERS = ["none", "firefox", "chrome", "edge", "brave", "chromium",
            "opera", "vivaldi", "safari", "whale"]

# Offered in the window; yt-dlp accepts any code the site actually publishes,
# and "all" takes everything it has.
SUBTITLE_LANGS = ["none", "en", "pt", "es", "fr", "de", "it", "ja", "ko", "ru", "zh", "all"]

# What SponsorBlock is asked to cut. The other categories it knows about are
# markers rather than stretches of video, so removing them means nothing.
SPONSOR_CATEGORIES = ["sponsor", "selfpromo", "interaction"]

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def find_preset(name):
    """Look a preset up by its window name or its command line name."""
    for preset in PRESETS:
        if name in (preset["name"], preset["cli"]):
            return preset
    return None


def describe_settings(settings):
    """One sentence saying what these choices will produce.

    Written from the settings themselves rather than stored next to each
    preset, so the description cannot drift away from what the preset does.
    """
    if settings.get("mode") == "audio":
        audio_format = settings.get("audio_format", "mp3")
        tagged = " with the cover art and tags embedded" if audio_format in (
            "mp3", "m4a", "flac") else ""
        if audio_format in ("wav", "flac"):
            return "Audio only: %s, lossless%s." % (audio_format.upper(), tagged)
        return "Audio only: %s at %s kbps%s." % (
            audio_format.upper(), settings.get("bitrate", "192"), tagged)

    quality = settings.get("quality", "Best")
    if quality == "Best":
        return ("Video: MP4 at the highest resolution the site offers, whatever the codec "
                "(so 4K stays possible).")
    return "Video: H.264 MP4 capped at %s, the codec every player and editor reads." % quality


def format_progress(stats, verbose=True):
    """Progress as text. `stats` is None once the bytes are all in."""
    if not stats:
        return "download finished, processing..."
    speed = "%.2f MB/s" % (stats["speed"] / 1048576) if stats["speed"] else "-- MB/s"
    eta = "ETA %ss" % stats["eta"] if stats["eta"] else ""
    if verbose:
        return "%.1f/%.1f MB  %s  %s" % (stats["done"] / 1048576,
                                         stats["total"] / 1048576, speed, eta)
    return "%s  %s" % (speed, eta)


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


# ================================================================== yt-dlp
# Sites change their pages and yt-dlp follows them, often within days. A copy
# more than a few weeks old is the single most common reason a download starts
# failing, and the error it produces ("unable to extract...") says nothing
# about that. So the app checks, and offers to fix it.
YTDLP_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"

# Failures that mean "the site moved and this copy of yt-dlp has not caught up",
# as opposed to a bad link or a private video. yt-dlp words them in its own
# vocabulary, which tells the user nothing about what to do next.
STALE_MARKERS = (
    "unable to extract",
    "failed to parse json",
    "nsig extraction",
    "signature extraction",
    "unable to download api page",
    "player response",
    "no video formats found",
    "http error 403",
)

UPDATE_HINT = ("A site changed and this copy of yt-dlp has not caught up. The fix is "
               "usually released within days:\n    python -m pip install -U yt-dlp")


def python_executable():
    """The interpreter to run pip with.

    run.bat starts the app with pythonw.exe so no console window appears. pip
    is a console program; the plain python.exe beside it is the sane thing to
    hand a pipe to.
    """
    executable = sys.executable
    if sys.platform == "win32" and os.path.basename(executable).lower() == "pythonw.exe":
        sibling = os.path.join(os.path.dirname(executable), "python.exe")
        if os.path.isfile(sibling):
            return sibling
    return executable


def parse_version(text):
    """'2026.08.19' -> (2026, 8, 19), and anything unparseable -> ().

    Never compare the strings. yt-dlp writes its date zero padded and PyPI
    normalises the padding away, so the identical release reads as 2026.08.19
    in one place and 2026.8.19 in the other.
    """
    parts = []
    for chunk in re.split(r"[._+-]", (text or "").strip()):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def installed_ytdlp_version(prefer_loaded=True):
    """The yt-dlp version on this machine, or None if it is not installed.

    Read from the package metadata instead of importing yt_dlp: importing it
    pins the old code in memory for the life of the process, and an upgrade the
    user just clicked should take effect without restarting the app.

    Once a download has run the module is loaded anyway, and then the honest
    answer is the one in memory -- unless the caller is checking what pip just
    wrote to disk, which is what prefer_loaded=False is for.
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
        return False, ("Could not update yt-dlp. Do it by hand with:\n"
                       "    python -m pip install -U yt-dlp")

    after = installed_ytdlp_version(prefer_loaded=False)
    if after and before and parse_version(after) == parse_version(before):
        return True, "yt-dlp was already up to date (%s)." % after
    if "yt_dlp" in sys.modules:
        # The old module is already loaded and will stay loaded until exit.
        return True, "yt-dlp updated to %s. Restart anydl to use it." % (after or "?")
    return True, "yt-dlp updated to %s." % (after or "?")


def open_in_file_manager(path):
    """Reveal a directory in the platform's file manager."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


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


class _LoggerAdapter:
    """Routes yt-dlp's own messages into the app log."""

    def __init__(self, on_log):
        self.on_log = on_log
        self._last_error = None

    def debug(self, msg):
        msg = ANSI.sub("", str(msg))
        if msg.startswith("[debug]") or not msg.strip():
            return
        self.on_log(msg)

    def info(self, msg):
        self.on_log(ANSI.sub("", str(msg)))

    def warning(self, msg):
        self.on_log("[warning] " + ANSI.sub("", str(msg)))

    def error(self, msg):
        text = ANSI.sub("", str(msg)).strip()
        # yt-dlp reports the same failure through several layers, sometimes
        # stacking its own "ERROR: " prefix. Collapse both.
        while text.upper().startswith("ERROR: "):
            text = text[7:].lstrip()
        if not text or text == self._last_error:
            return
        self._last_error = text
        self.on_log("[error] " + text)


class Converter:
    """Wraps yt-dlp and reports progress through callbacks."""

    def __init__(self, on_log=print, on_progress=None, on_done=None,
                 browser=None, cookie_file=None, update_hint=None, on_title=None,
                 on_stage=None):
        self.on_log = on_log
        self.on_progress = on_progress or (lambda pct, stats: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self.on_title = on_title or (lambda title: None)
        self.on_stage = on_stage or (lambda stage: None)
        self.cancelled = False
        self._last_emit = 0.0
        self.ffmpeg_dir = find_ffmpeg()
        # What to say when a failure smells like a stale extractor. The window
        # replaces this with wording the user can act on without leaving it.
        self.update_hint = update_hint or UPDATE_HINT
        # Where to borrow a logged-in session from, if anywhere.
        self.browser = None if (browser or "none") == "none" else browser
        self.cookie_file = cookie_file

    def _explain(self, exc):
        """Turn yt-dlp's terser failures into something actionable."""
        text = str(exc)
        low = text.lower()

        if self.browser and ("cookie" in low or "permission denied" in low):
            # On Windows a running Chromium browser keeps its cookie database
            # locked, so yt-dlp cannot even copy it. Firefox is unaffected.
            return (
                "Could not read the cookies from %s. Close %s completely, including "
                "any icon left in the system tray, then try again. Firefox does not "
                "lock its cookies, and a cookies.txt file always works."
                % (self.browser, self.browser)
            )
        if "log-in" in low or "logged-in" in low or "login required" in low:
            return (
                text + "\n\nThis site wants an account. Pick your browser under "
                "'Sign-in cookies' (or pass --cookies-from-browser) and retry."
            )
        if any(marker in low for marker in STALE_MARKERS):
            return text + "\n\n" + self.update_hint
        return text

    def _apply_cookies(self, opts):
        """Attach browser or file cookies to a yt-dlp options dict."""
        if self.cookie_file:
            opts["cookiefile"] = self.cookie_file
        if self.browser:
            # yt-dlp expects (browser, profile, keyring, container).
            opts["cookiesfrombrowser"] = (self.browser, None, None, None)
        return opts

    # ------------------------------------------------------------------ hooks
    def _progress_hook(self, d):
        if self.cancelled:
            raise KeyboardInterrupt("cancelled by user")
        status = d.get("status")
        if status == "downloading":
            # The hook fires per chunk. Publishing every one of them floods the
            # event queue and the window spends its time redrawing text.
            now = time.monotonic()
            if now - self._last_emit < 0.15:
                return
            self._last_emit = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100) if total else 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            self.on_title(self._describe_file(d))
            # The numbers, not a sentence: the terminal has a whole line to
            # spend on them and a queue row has one narrow column.
            self.on_progress(pct, {"done": done, "total": total,
                                   "speed": speed, "eta": eta})
        elif status == "finished":
            self._last_emit = 0.0
            self.on_progress(100, None)

    def _describe_file(self, d):
        """A name for what is being downloaded right now, for the queue line."""
        info = d.get("info_dict") or {}
        title = info.get("title") or os.path.basename(d.get("filename") or "")
        index, total = info.get("playlist_index"), info.get("n_entries")
        if index and total:
            return "%s  (%s of %s)" % (title, index, total)
        return title

    def _postprocessor_hook(self, d):
        if d.get("status") != "started":
            return
        if self.cancelled:
            # ffmpeg has the file now; stop at the boundary between stages
            # rather than in the middle of one.
            raise KeyboardInterrupt("cancelled by user")
        stage = str(d.get("postprocessor", ""))
        self.on_log("[convert] " + stage)
        self.on_stage(stage)

    # ---------------------------------------------------------------- options
    def _build_options(self, settings):
        out_dir = settings["out_dir"]
        mode = settings.get("mode", "video")
        playlist = settings.get("playlist", False)
        if playlist:
            # The trailing | is the empty default: a link that turns out not to
            # be a playlist would otherwise land in a folder called "NA", and
            # yt-dlp drops the empty path component for us.
            template = os.path.join(out_dir, "%(playlist_title|)s", "%(title)s.%(ext)s")
        else:
            template = os.path.join(out_dir, "%(title)s.%(ext)s")

        opts = {
            "outtmpl": template,
            "noplaylist": not playlist,
            "ignoreerrors": playlist,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _LoggerAdapter(self.on_log),
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
        }

        if self.ffmpeg_dir:
            opts["ffmpeg_location"] = self.ffmpeg_dir

        self._apply_cookies(opts)

        # The order of this list is the order ffmpeg touches the file, and it
        # follows the order yt-dlp's own command line builds: cut the sponsor
        # segments out first, convert, then write the tags onto what is left.
        stages = []
        sponsorblock = bool(settings.get("sponsorblock")) and bool(self.ffmpeg_dir)
        if sponsorblock:
            stages.append({"key": "SponsorBlock",
                           "categories": set(SPONSOR_CATEGORIES),
                           "when": "after_filter"})

        if mode == "audio":
            self._audio_options(opts, stages, settings.get("audio_format", "mp3"),
                                settings.get("bitrate", "192"))
        else:
            self._video_options(opts, settings.get("quality", "Best"))

        if self.ffmpeg_dir:
            self._subtitle_options(opts, stages, settings, mode)
            if sponsorblock:
                stages.append({"key": "ModifyChapters",
                               "remove_sponsor_segments": list(SPONSOR_CATEGORIES)})
            stages.append({"key": "FFmpegMetadata"})
            if opts.get("writethumbnail"):
                # already_have_thumbnail=False tells yt-dlp the loose image
                # file is its own to delete once it is inside the audio file.
                stages.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
            opts["postprocessors"] = stages
        return opts

    def _audio_options(self, opts, stages, audio_format, bitrate):
        if not self.ffmpeg_dir:
            # No ffmpeg means no transcoding: keep the original stream as-is.
            opts["format"] = "bestaudio[ext=m4a]/bestaudio"
            self.on_log("[warning] ffmpeg missing: saving the original audio without converting.")
            return

        opts["format"] = "bestaudio/best"
        stages.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": bitrate,
        })
        if audio_format in ("mp3", "m4a", "flac"):
            opts["writethumbnail"] = True

    def _subtitle_options(self, opts, stages, settings, mode):
        """Subtitles as a file beside the video, or inside it."""
        langs = settings.get("subtitles") or "none"
        if langs == "none" or mode == "audio":
            return

        opts["writesubtitles"] = True
        # Most of YouTube only has machine-made captions. Asking for those too
        # costs nothing: where a real subtitle exists yt-dlp still prefers it.
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = [chunk.strip() for chunk in langs.split(",") if chunk.strip()]

        if settings.get("embed_subs"):
            # already_have_subtitle=False lets yt-dlp remove the loose file
            # once the track is in the container, as --embed-subs does.
            stages.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
        else:
            # A .srt opens in anything; the .vtt YouTube hands out does not.
            opts["subtitlesformat"] = "srt/best"
            stages.append({"key": "FFmpegSubtitlesConvertor", "format": "srt",
                           "when": "before_dl"})

    def _video_options(self, opts, quality):
        height = None if quality == "Best" else quality.rstrip("p")

        if not self.ffmpeg_dir:
            # Without ffmpeg the video and audio tracks cannot be merged, so we
            # are limited to whatever pre-muxed stream exists (usually 720p).
            if height:
                opts["format"] = "best[height<=%s][ext=mp4]/best[height<=%s]/best" % (
                    height,
                    height,
                )
            else:
                opts["format"] = "best[ext=mp4]/best"
            self.on_log("[warning] ffmpeg missing: limited to a single stream (usually 720p).")
            return

        if height:
            # Prefer avc1 (H.264) so the file plays in any player or editor.
            # AV1/VP9 only win when no H.264 rendition exists at that height.
            opts["format"] = (
                "bestvideo[height<=%s][vcodec^=avc1]+bestaudio[ext=m4a]/"
                "bestvideo[height<=%s][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=%s]+bestaudio/"
                "best[height<=%s]/best" % (height, height, height, height)
            )
        else:
            # "Best" deliberately ignores the codec: YouTube, for one, only
            # serves H.264
            # up to 1080p, so filtering on avc1 here would throw 4K away.
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

        opts["merge_output_format"] = "mp4"

    # ----------------------------------------------------------------- public
    def probe(self, url):
        """Fetch metadata for a single URL without downloading anything."""
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        self._apply_cookies(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def run(self, url, settings):
        """Download one link. Returns (ok, message) and never raises.

        One link at a time is what the queue needs: every item carries its own
        settings, so each gets its own YoutubeDL, and a failure or a cancel
        stops that item alone.
        """
        try:
            import yt_dlp
        except ImportError:
            return False, "yt-dlp is not installed. Run: python -m pip install -U yt-dlp"
        if self.cancelled:
            return False, "Cancelled."

        out_dir = settings["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        self._last_emit = 0.0
        try:
            with yt_dlp.YoutubeDL(self._build_options(settings)) as ydl:
                ydl.download([url])
        except KeyboardInterrupt:
            return False, "Cancelled."
        except Exception as exc:  # noqa: BLE001
            return False, self._explain(exc)
        return True, "Saved to " + out_dir

    def announce_cookies(self):
        if self.browser:
            self.on_log("[cookies] using the signed-in session from " + self.browser)
        elif self.cookie_file:
            self.on_log("[cookies] using " + os.path.basename(self.cookie_file))

    def download(self, urls, out_dir, mode="video", quality="Best",
                 audio_format="mp3", bitrate="192", playlist=False, extras=None):
        """Download a list of links one after another (used by the CLI)."""
        settings = {"out_dir": out_dir, "mode": mode, "quality": quality,
                    "audio_format": audio_format, "bitrate": bitrate, "playlist": playlist}
        settings.update(extras or {})

        self.cancelled = False
        self.announce_cookies()
        failures = 0

        for url in urls:
            if self.cancelled:
                break
            self.on_log("\n>> " + url)
            ok, message = self.run(url, settings)
            if ok:
                continue
            if self.cancelled:
                self.on_done(False, "Cancelled.")
                return
            failures += 1
            self.on_log("[error] " + message)

        if failures:
            self.on_done(True, "Finished with %d error(s). Saved to %s" % (failures, out_dir))
        else:
            self.on_done(True, "Done! Saved to " + out_dir)


# ====================================================================== GUI
# tkinter is imported on demand. Several Linux distributions package it apart
# from Python, and the command line mode has to keep working without it.
tk = ttk = filedialog = messagebox = None

# The update strip. Amber rather than red: nothing is broken yet.
BANNER_BG = "#fdf3c9"
BANNER_FG = "#4a3a00"


def _load_tk():
    global tk, ttk, filedialog, messagebox
    import tkinter
    from tkinter import ttk as ttk_module
    from tkinter import filedialog as filedialog_module
    from tkinter import messagebox as messagebox_module

    tk = tkinter
    ttk = ttk_module
    filedialog = filedialog_module
    messagebox = messagebox_module


class QueueItem:
    """One line of the queue.

    Carries a copy of the choices as they were when it was added, so a queue
    can hold an MP3 and a 1080p video at once and changing a dropdown
    afterwards does not rewrite what is already waiting.
    """

    def __init__(self, uid, url, settings):
        self.uid = uid
        self.url = url
        self.settings = settings
        self.label = url  # replaced by the real title once yt-dlp reports it
        self.status = "Queued"
        self.progress = 0.0
        self.detail = ""
        self.cancelled = False
        self.converter = None

    @property
    def finished(self):
        return self.status in ("Done", "Failed", "Cancelled")


def progress_bar(pct):
    """A ten-cell bar in text, since a Treeview cell cannot hold a widget."""
    filled = int(round(max(0.0, min(100.0, pct)) / 10.0))
    return "[" + "#" * filled + "-" * (10 - filled) + "]"


class AnydlApp:
    """The window.

    Downloads run on a worker thread, and a worker thread must never touch a Tk
    widget. Workers publish events onto `self.events` instead, and `pump()`
    drains that queue on the main thread every 120 ms.
    """

    def __init__(self):
        self.events = queue.Queue()
        self.fallback_dir = default_output_dir()
        self.ffmpeg_dir = find_ffmpeg()
        # The queue: `items` is the worker's view (guarded by the lock), `rows`
        # is the window's, keyed by the same uid the Treeview uses for its row.
        self.items = []
        self.rows = {}
        self.lock = threading.Lock()
        self.worker_running = False
        self.next_uid = 0
        self.hint = UPDATE_HINT
        self.applying_preset = False
        self.checking = False
        self.installed_version = None
        self.latest_version = None

        self.window = tk.Tk()
        self.window.title(APP_NAME + " - video and audio downloader")
        # A fixed height puts the footer under the taskbar on a 1080p screen
        # and off the bottom entirely on a 1366x768 laptop.
        height = min(760, max(520, self.window.winfo_screenheight() - 200))
        self.window.geometry("800x%d" % height)
        self.window.minsize(700, 500)
        apply_window_icon(self.window)

        self.top = ttk.Frame(self.window, padding=12)
        self.top.pack(fill="x")
        self._build_banner(self.window)
        self._build_links(self.top)
        self._build_presets(self.top)
        self._build_choices(self.top)
        self._build_extras(self.top)
        self._build_login(self.top)
        self._build_destination(self.top)
        self._build_buttons(self.top)

        # Packed before the body so the body's expand does not squeeze it out.
        self._build_footer(self.window)

        body = ttk.Frame(self.window, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        self._build_progress(body)
        # The split is draggable: some sessions are about watching the queue,
        # others about reading why one line failed.
        split = ttk.PanedWindow(body, orient="vertical")
        split.pack(fill="both", expand=True, pady=(8, 0))
        self._build_queue(split)
        self._build_log(split)

        self.setting_vars = {
            "mode": self.mode_var,
            "quality": self.quality_var,
            "audio_format": self.format_var,
            "bitrate": self.bitrate_var,
        }
        for variable in self.setting_vars.values():
            variable.trace_add("write", self.on_manual_change)
        self.mode_var.trace_add("write", self.sync_fields)
        self.sync_fields()
        self.describe_preset()

        if self.ffmpeg_dir:
            self.log("ffmpeg detected.")
        else:
            self.log("[warning] ffmpeg not found: MP3 output and 1080p+ are unavailable. "
                     "See the README for how to install it.")

    def run(self):
        self.pump()
        self.check_for_updates()  # in the background; the window is already up
        self.window.mainloop()

    # ------------------------------------------------------------------ build
    def _build_banner(self, parent):
        """A strip that stays hidden until yt-dlp needs attention."""
        self.banner = tk.Frame(parent, background=BANNER_BG)
        inner = tk.Frame(self.banner, background=BANNER_BG)
        inner.pack(fill="x", padx=12, pady=8)

        self.banner_var = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.banner_var, background=BANNER_BG,
                 foreground=BANNER_FG, anchor="w", justify="left",
                 wraplength=520).pack(side="left", fill="x", expand=True)
        self.dismiss_btn = ttk.Button(inner, text="Later", command=self.hide_banner)
        self.dismiss_btn.pack(side="right")
        self.update_btn = ttk.Button(inner, text="Update now", command=self.start_update)
        self.update_btn.pack(side="right", padx=(8, 6))

    def _build_footer(self, parent):
        footer = ttk.Frame(parent, padding=(12, 0, 12, 10))
        footer.pack(fill="x", side="bottom")
        self.footer_var = tk.StringVar(value="checking yt-dlp...")
        ttk.Label(footer, textvariable=self.footer_var,
                  foreground="#777777").pack(side="left")
        self.check_btn = ttk.Button(footer, text="Check for updates",
                                    command=lambda: self.check_for_updates(announce=True))
        self.check_btn.pack(side="right")

    def _build_links(self, parent):
        ttk.Label(parent, text="Link(s), one per line:").pack(anchor="w")
        self.urls_box = tk.Text(parent, height=4, wrap="none")
        self.urls_box.pack(fill="x", pady=(4, 10))

    def _build_presets(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="Preset:").pack(side="left")

        self.preset_var = tk.StringVar(value=CUSTOM_PRESET)
        for preset in PRESETS:
            button = ttk.Radiobutton(row, text=preset["name"], value=preset["name"],
                                     variable=self.preset_var, command=self.apply_preset)
            button.pack(side="left", padx=(10, 0))
            # Hovering explains a preset before committing to it.
            button.bind("<Enter>", lambda _event, chosen=preset: self.preview_preset(chosen))
            button.bind("<Leave>", lambda _event: self.describe_preset())
        ttk.Radiobutton(row, text=CUSTOM_PRESET, value=CUSTOM_PRESET,
                        variable=self.preset_var,
                        command=self.describe_preset).pack(side="left", padx=(10, 0))

        self.preset_note = ttk.Label(parent, text="", foreground="#777777")
        self.preset_note.pack(anchor="w", pady=(3, 8))

    def _build_choices(self, parent):
        choices = ttk.Frame(parent)
        choices.pack(fill="x")

        self.mode_var = tk.StringVar(value="video")
        ttk.Radiobutton(choices, text="Video (MP4)", value="video",
                        variable=self.mode_var).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(choices, text="Audio only", value="audio",
                        variable=self.mode_var).grid(row=0, column=1, sticky="w", padx=(16, 0))

        self.playlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(choices, text="Download the whole playlist",
                        variable=self.playlist_var).grid(row=0, column=2, sticky="w", padx=(24, 0))

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))

        ttk.Label(row, text="Quality:").grid(row=0, column=0, sticky="w")
        self.quality_var = tk.StringVar(value="Best")
        self.quality_box = ttk.Combobox(row, values=VIDEO_QUALITIES, width=10,
                                        state="readonly", textvariable=self.quality_var)
        self.quality_box.grid(row=0, column=1, padx=(6, 20))

        ttk.Label(row, text="Audio format:").grid(row=0, column=2, sticky="w")
        self.format_var = tk.StringVar(value="mp3")
        self.format_box = ttk.Combobox(row, values=AUDIO_FORMATS, width=8,
                                       state="readonly", textvariable=self.format_var)
        self.format_box.grid(row=0, column=3, padx=(6, 20))

        ttk.Label(row, text="Bitrate:").grid(row=0, column=4, sticky="w")
        self.bitrate_var = tk.StringVar(value="192")
        self.bitrate_box = ttk.Combobox(row, values=AUDIO_BITRATES, width=6,
                                        state="readonly", textvariable=self.bitrate_var)
        self.bitrate_box.grid(row=0, column=5, padx=(6, 0))

    def _build_extras(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))

        ttk.Label(row, text="Subtitles:").pack(side="left")
        self.subs_var = tk.StringVar(value="none")
        self.subs_box = ttk.Combobox(row, values=SUBTITLE_LANGS, width=6,
                                     state="readonly", textvariable=self.subs_var)
        self.subs_box.pack(side="left", padx=(6, 8))
        self.subs_var.trace_add("write", self.sync_fields)

        self.embed_subs_var = tk.BooleanVar(value=True)
        self.embed_subs_check = ttk.Checkbutton(row, text="inside the video, not a .srt file",
                                                variable=self.embed_subs_var)
        self.embed_subs_check.pack(side="left")

        self.sponsor_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Cut sponsor segments (SponsorBlock)",
                        variable=self.sponsor_var).pack(side="left", padx=(24, 0))

    def _build_login(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))

        ttk.Label(row, text="Sign-in cookies:").pack(side="left")
        self.browser_var = tk.StringVar(value="none")
        ttk.Combobox(row, values=BROWSERS, width=10, state="readonly",
                     textvariable=self.browser_var).pack(side="left", padx=(6, 8))
        ttk.Label(row, text="borrow a logged-in session for sites like Instagram or Vimeo",
                  foreground="#777777").pack(side="left")

        self.cookie_file_var = tk.StringVar(value="")
        ttk.Button(row, text="cookies.txt...",
                   command=self.pick_cookie_file).pack(side="right")

    def _build_destination(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Save to:").pack(side="left")
        self.dest_var = tk.StringVar(value=self.fallback_dir)
        ttk.Entry(row, textvariable=self.dest_var).pack(side="left", fill="x",
                                                        expand=True, padx=6)
        ttk.Button(row, text="...", width=4, command=self.choose_folder).pack(side="left")
        ttk.Button(row, text="Open folder",
                   command=self.open_folder).pack(side="left", padx=(6, 0))

    def _build_buttons(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(12, 0))
        self.download_btn = ttk.Button(row, text="Download", command=self.start)
        self.download_btn.pack(side="left")
        ttk.Button(row, text="Cancel selected",
                   command=self.cancel_selected).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Cancel all", command=self.cancel_all).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Clear finished",
                   command=self.clear_finished).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Clear log", command=self.clear_log).pack(side="right")

    def _build_progress(self, parent):
        self.bar = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(8, 4))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(parent, textvariable=self.status_var).pack(anchor="w")

    def _build_queue(self, parent):
        frame = ttk.Frame(parent)
        parent.add(frame, weight=3)

        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        self.tree = ttk.Treeview(frame, columns=("status", "progress"),
                                 show="tree headings", height=9,
                                 selectmode="extended", yscrollcommand=scrollbar.set)
        self.tree.heading("#0", text="Link")
        self.tree.heading("status", text="Status")
        self.tree.heading("progress", text="Progress")
        self.tree.column("#0", width=330, minwidth=160, stretch=True)
        self.tree.column("status", width=100, minwidth=80, stretch=False, anchor="w")
        self.tree.column("progress", width=280, minwidth=180, stretch=False, anchor="w")
        # A proportional font turns the text bar into a ragged line.
        ttk.Style().configure("Queue.Treeview", font="TkFixedFont")
        self.tree.configure(style="Queue.Treeview")
        self.tree.tag_configure("Done", foreground="#1a7f37")
        self.tree.tag_configure("Failed", foreground="#b42318")
        self.tree.tag_configure("Cancelled", foreground="#888888")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.tree.yview)

        self.tree.bind("<Double-1>", self.open_item_folder)
        self.tree.bind("<Delete>", lambda _event: self.clear_selected())

    def _build_log(self, parent):
        frame = ttk.Frame(parent)
        parent.add(frame, weight=2)
        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        self.log_box = tk.Text(frame, height=6, wrap="word", state="disabled",
                               background="#111111", foreground="#dddddd",
                               insertbackground="#dddddd", yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.log_box.yview)

    # ---------------------------------------------------------------- widgets
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ---------------------------------------------------------------- presets
    def current_settings(self):
        """The choices as they stand, as a plain dict."""
        return {
            "mode": self.mode_var.get(),
            "quality": self.quality_var.get(),
            "audio_format": self.format_var.get(),
            "bitrate": self.bitrate_var.get(),
        }

    def apply_preset(self):
        preset = find_preset(self.preset_var.get())
        if preset is None:
            return
        self.applying_preset = True  # keeps the change from reading as manual
        try:
            for name, value in preset["settings"].items():
                self.setting_vars[name].set(value)
        finally:
            self.applying_preset = False
        self.describe_preset()

    def on_manual_change(self, *_args):
        """Touching a dropdown means the choices are the user's own now."""
        if self.applying_preset:
            return
        self.preset_var.set(CUSTOM_PRESET)
        self.describe_preset()

    def describe_preset(self, *_args):
        preset = find_preset(self.preset_var.get())
        settings = dict(self.current_settings(), **preset["settings"]) if preset \
            else self.current_settings()
        self.preset_note.configure(text=describe_settings(settings))

    def preview_preset(self, preset):
        self.preset_note.configure(
            text=describe_settings(dict(self.current_settings(), **preset["settings"])))

    def sync_fields(self, *_args):
        audio = self.mode_var.get() == "audio"
        self.quality_box.configure(state="disabled" if audio else "readonly")
        self.format_box.configure(state="readonly" if audio else "disabled")
        self.bitrate_box.configure(state="readonly" if audio else "disabled")
        # An audio file has nowhere to put a subtitle track.
        self.subs_box.configure(state="disabled" if audio else "readonly")
        self.embed_subs_check.configure(
            state="disabled" if audio or self.subs_var.get() == "none" else "normal")

    def pick_cookie_file(self):
        chosen = filedialog.askopenfilename(
            title="Select a cookies.txt file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        self.cookie_file_var.set(chosen or "")
        if chosen:
            self.log("[cookies] file selected: " + os.path.basename(chosen))
            self.browser_var.set("none")

    def choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or self.fallback_dir)
        if chosen:
            self.dest_var.set(chosen)

    def open_folder(self):
        target = self.dest_var.get()
        if os.path.isdir(target):
            open_in_file_manager(target)
        else:
            messagebox.showinfo(APP_NAME, "That folder does not exist yet.")

    # ----------------------------------------------------------------- yt-dlp
    def check_for_updates(self, announce=False):
        """Ask PyPI what the current yt-dlp is, off the Tk thread."""
        if self.checking:
            return
        self.checking = True
        self.check_btn.configure(state="disabled")
        threading.Thread(target=self._check_worker, args=(announce,), daemon=True).start()

    def _check_worker(self, announce):
        result = {"announce": announce, "latest": None, "problem": None,
                  "detail": None, "installed": installed_ytdlp_version()}
        if result["installed"] is None:
            result["problem"] = "not installed"
        else:
            try:
                result["latest"] = latest_ytdlp_version()
            except Exception as exc:  # noqa: BLE001
                # Being offline is normal and not worth a dialog; the footer
                # says the check did not happen and the log says why.
                result["problem"] = "update check failed"
                result["detail"] = str(exc)
        self.events.put(("engine", result))

    def on_engine(self, result):
        self.checking = False
        self.check_btn.configure(state="normal")
        self.installed_version = result["installed"]
        self.latest_version = result["latest"]
        self.refresh_footer(result["problem"])
        self.hint = self.stale_hint()  # what a failing item will be told to say
        if result["detail"]:
            self.log("[update] could not reach PyPI: " + result["detail"])

        if result["installed"] is None:
            self.show_banner("yt-dlp is not installed, so nothing can be downloaded yet.",
                             "Install now")
            return
        if result["latest"] and parse_version(result["latest"]) > parse_version(result["installed"]):
            self.show_banner(
                "yt-dlp %s is out and you have %s. Downloads that started failing usually "
                "work again after this." % (result["latest"], result["installed"]),
                "Update now")
        elif result["announce"] and result["latest"]:
            # Only claim this when PyPI actually answered.
            self.log("[update] yt-dlp %s is the current release." % result["installed"])

    def refresh_footer(self, problem=None):
        parts = ["yt-dlp " + self.installed_version if self.installed_version
                 else "yt-dlp missing"]
        parts.append("ffmpeg found" if self.ffmpeg_dir else "no ffmpeg")
        if problem and problem != "not installed":
            parts.append(problem)
        self.footer_var.set("   |   ".join(parts))

    def show_banner(self, text, button_label):
        self.banner_var.set(text)
        self.update_btn.configure(text=button_label, state="normal")
        self.dismiss_btn.configure(text="Later")
        if not self.update_btn.winfo_ismapped():
            self.update_btn.pack(side="right", padx=(8, 6))
        self.banner.pack(fill="x", side="top", before=self.top)

    def hide_banner(self):
        self.banner.pack_forget()

    def start_update(self):
        self.update_btn.configure(state="disabled")
        self.banner_var.set("Updating yt-dlp...")
        self.log("[update] python -m pip install -U yt-dlp")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        ok, message = update_ytdlp(lambda line: self.events.put(("log", line)))
        self.events.put(("updated", (ok, message)))

    def on_updated(self, ok, message):
        self.log("[update] " + message)
        self.banner_var.set(message)
        if ok:
            self.update_btn.pack_forget()
            self.dismiss_btn.configure(text="OK")
            self.installed_version = installed_ytdlp_version()
            self.latest_version = self.installed_version
            self.refresh_footer()
            self.hint = self.stale_hint()
        else:
            self.update_btn.configure(state="normal")

    def stale_hint(self):
        """What to tell a worker to say when an extractor looks out of date."""
        if (self.latest_version and self.installed_version
                and parse_version(self.latest_version) > parse_version(self.installed_version)):
            return ("yt-dlp %s is out and you are on %s. Click '%s' at the top of the window, "
                    "then try this link again."
                    % (self.latest_version, self.installed_version, self.update_btn.cget("text")))
        return UPDATE_HINT

    # ----------------------------------------------------------------- events
    def pump(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "engine":
                    self.on_engine(payload)
                elif kind == "updated":
                    self.on_updated(*payload)
                elif kind == "item":
                    item = payload
                    self.refresh_row(item)
                    self.refresh_status()
                    if item.finished:
                        self.bar["value"] = 100 if item.status == "Done" else 0
                elif kind == "item-progress":
                    item, pct, stats = payload
                    item.progress = pct
                    item.detail = format_progress(stats, verbose=False)
                    self.refresh_row(item)
                    self.bar["value"] = pct  # the bar follows whatever is running
                elif kind == "item-title":
                    item, title = payload
                    if title and title != item.label:
                        item.label = title
                        self.refresh_row(item)
                elif kind == "item-stage":
                    item, stage = payload
                    if item.status == "Downloading":
                        item.status, item.progress = "Converting", 100.0
                        self.refresh_status()
                    item.detail = stage
                    self.refresh_row(item)
                elif kind == "queue-idle":
                    self.refresh_status()
        except queue.Empty:
            pass
        self.window.after(120, self.pump)

    # ------------------------------------------------------------------ queue
    def start(self):
        """Put the links in the box on the queue and make sure it is moving."""
        raw = self.urls_box.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning(APP_NAME, "Paste at least one link.")
            return

        out_dir = self.dest_var.get().strip() or self.fallback_dir
        self.dest_var.set(out_dir)
        settings = dict(self.current_settings(),
                        out_dir=out_dir,
                        playlist=self.playlist_var.get(),
                        subtitles=self.subs_var.get(),
                        embed_subs=self.embed_subs_var.get(),
                        sponsorblock=self.sponsor_var.get(),
                        browser=self.browser_var.get(),
                        cookie_file=self.cookie_file_var.get() or None)

        for url in urls:
            self.next_uid += 1
            item = QueueItem(self.next_uid, url, dict(settings))
            with self.lock:
                self.items.append(item)
            self.rows[item.uid] = item
            self.tree.insert("", "end", iid=str(item.uid), text=item.label,
                             values=("Queued", ""))
        # The links live in the queue now; leaving them in the box only invites
        # adding them a second time.
        self.urls_box.delete("1.0", "end")
        self.tree.see(str(self.next_uid))
        self.refresh_status()
        self.ensure_worker()

    def ensure_worker(self):
        with self.lock:
            if self.worker_running:
                return
            self.worker_running = True
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _next_item(self):
        """Claim the next waiting item, or release the worker if there is none.

        Claiming and standing down both happen under the lock that start()
        takes, so a link added while the worker was winding up cannot be left
        sitting in the queue with nothing running.
        """
        with self.lock:
            for item in self.items:
                if item.status == "Queued" and not item.cancelled:
                    item.status = "Downloading"
                    return item
            self.worker_running = False
            return None

    def _queue_worker(self):
        while True:
            item = self._next_item()
            if item is None:
                self.events.put(("queue-idle", None))
                return

            converter = Converter(
                on_log=lambda msg: self.events.put(("log", str(msg))),
                on_progress=lambda pct, stats, this=item: self.events.put(
                    ("item-progress", (this, pct, stats))),
                on_title=lambda title, this=item: self.events.put(("item-title", (this, title))),
                on_stage=lambda stage, this=item: self.events.put(("item-stage", (this, stage))),
                browser=item.settings.get("browser"),
                cookie_file=item.settings.get("cookie_file"),
                update_hint=self.hint,
            )
            with self.lock:
                # A cancel that arrived while this was being set up would have
                # had nothing to set the flag on.
                converter.cancelled = item.cancelled
                item.converter = converter

            self.events.put(("item", item))
            self.events.put(("log", "\n>> " + item.url))
            converter.announce_cookies()
            ok, message = converter.run(item.url, item.settings)

            with self.lock:
                item.converter = None
                if item.cancelled:
                    item.status, item.detail = "Cancelled", "Cancelled."
                elif ok:
                    item.status, item.progress, item.detail = "Done", 100.0, message
                else:
                    item.status, item.detail = "Failed", message
            if item.status == "Failed":
                self.events.put(("log", "[error] " + message))
            elif item.status == "Cancelled" and item.progress > 0:
                # yt-dlp keeps what it had as a .part file, on purpose: queueing
                # the same link again picks up where this left off.
                self.events.put(("log", "[cancelled] %s - the partial file is still in the "
                                        "folder, and downloading it again resumes from there."
                                 % item.label))
            self.events.put(("item", item))

    def cancel_item(self, item):
        if item.finished:
            return
        with self.lock:
            item.cancelled = True
            if item.status == "Queued":
                item.status = "Cancelled"
            else:
                item.status = "Cancelling"
                if item.converter:
                    item.converter.cancelled = True
        self.refresh_row(item)
        self.refresh_status()

    def cancel_selected(self):
        for uid in self.tree.selection():
            item = self.rows.get(int(uid))
            if item:
                self.cancel_item(item)

    def cancel_all(self):
        for item in list(self.rows.values()):
            self.cancel_item(item)

    def clear_finished(self):
        self._remove([item for item in self.rows.values() if item.finished])

    def clear_selected(self):
        chosen = [self.rows[int(uid)] for uid in self.tree.selection() if int(uid) in self.rows]
        self._remove([item for item in chosen if item.finished])

    def _remove(self, doomed):
        if not doomed:
            return
        with self.lock:
            for item in doomed:
                self.items.remove(item)
        for item in doomed:
            self.rows.pop(item.uid, None)
            self.tree.delete(str(item.uid))
        self.refresh_status()

    def open_item_folder(self, _event=None):
        for uid in self.tree.selection():
            item = self.rows.get(int(uid))
            if item and os.path.isdir(item.settings["out_dir"]):
                open_in_file_manager(item.settings["out_dir"])
            return

    # ------------------------------------------------------------- queue view
    def refresh_row(self, item):
        row = str(item.uid)
        if not self.tree.exists(row):
            return
        detail = item.detail.splitlines()[0] if item.detail else ""
        if item.status in ("Downloading", "Converting"):
            detail = "%s %5.1f%%  %s" % (progress_bar(item.progress), item.progress, detail)
        elif item.status == "Done":
            detail = progress_bar(100) + " done"
        self.tree.item(row, text=item.label, values=(item.status, detail),
                       tags=(item.status,))

    def refresh_status(self):
        counts = {}
        for item in self.rows.values():
            counts[item.status] = counts.get(item.status, 0) + 1
        if not counts:
            self.status_var.set("Ready.")
            return
        order = ["Downloading", "Converting", "Cancelling", "Queued",
                 "Done", "Failed", "Cancelled"]
        self.status_var.set("   ".join(
            "%d %s" % (counts[name], name.lower()) for name in order if counts.get(name)))


def launch_gui():
    claim_taskbar_identity()  # must happen before the first window exists
    _load_tk()
    AnydlApp().run()


# ====================================================================== CLI
def report_versions():
    """What the app is actually running with, for a bug report or a check."""
    installed = installed_ytdlp_version()
    print("%s using yt-dlp %s" % (APP_NAME, installed or "(not installed)"))
    ffmpeg_dir = find_ffmpeg()
    print("ffmpeg: " + (ffmpeg_dir if ffmpeg_dir else "not found"))
    if not installed:
        return
    try:
        latest = latest_ytdlp_version()
    except Exception as exc:  # noqa: BLE001
        print("could not check for a newer yt-dlp: %s" % exc)
        return
    if parse_version(latest) > parse_version(installed):
        print("yt-dlp %s is available. Update with: %s --update" % (latest, APP_NAME))
    else:
        print("yt-dlp is up to date.")


def main():
    parser = argparse.ArgumentParser(
        prog="anydl",
        description="Download video as MP4 from any site yt-dlp supports, "
                    "or extract just the audio track.",
    )
    parser.add_argument("url", nargs="*", help="link(s) to download")
    # These default to None so a preset can fill them in and an explicit flag
    # can still win over the preset.
    parser.add_argument("-p", "--preset", choices=[p["cli"] for p in PRESETS],
                        help="a set of ready-made choices: "
                             + ", ".join("%s (%s)" % (p["cli"], p["name"]) for p in PRESETS))
    parser.add_argument("-a", "--audio", action="store_true", default=None,
                        help="extract audio only")
    parser.add_argument("-f", "--format", default=None, choices=AUDIO_FORMATS,
                        dest="audio_format", help="audio format (default: mp3)")
    parser.add_argument("-b", "--bitrate", default=None, help="audio bitrate in kbps")
    parser.add_argument("-q", "--quality", default=None,
                        help="video quality: Best, 1080p, 720p, ...")
    parser.add_argument("-o", "--output", default=default_output_dir(),
                        help="destination folder")
    parser.add_argument("--playlist", action="store_true", help="download the whole playlist")
    parser.add_argument("--subs", metavar="LANG", default=None,
                        help="also fetch subtitles: a language code (en, pt, ...), "
                             "a comma separated list, or all")
    parser.add_argument("--embed-subs", dest="embed_subs", action="store_true",
                        help="put the subtitles inside the video instead of a .srt beside it")
    parser.add_argument("--sponsorblock", action="store_true",
                        help="cut sponsor, self-promotion and reminder segments out "
                             "of the file (YouTube, via the SponsorBlock database)")
    parser.add_argument("--cookies-from-browser", dest="browser", choices=BROWSERS,
                        default="none",
                        help="borrow the signed-in session from a local browser "
                             "(needed for Instagram, Vimeo and other gated sites)")
    parser.add_argument("--cookies", dest="cookie_file", default=None,
                        help="path to a cookies.txt file, as an alternative to "
                             "--cookies-from-browser")
    parser.add_argument("--update", action="store_true",
                        help="update yt-dlp to the current release and exit")
    parser.add_argument("--version", action="store_true",
                        help="show which yt-dlp and ffmpeg are in use, and exit")
    args = parser.parse_args()

    if args.version:
        report_versions()
        return

    if args.update:
        ok, message = update_ytdlp()
        print(message)
        sys.exit(0 if ok else 1)

    if not args.url:
        launch_gui()
        return

    settings = {"mode": "video", "quality": "Best", "audio_format": "mp3", "bitrate": "192"}
    if args.preset:
        settings.update(find_preset(args.preset)["settings"])
    if args.audio:
        settings["mode"] = "audio"
    if args.audio_format:
        settings["audio_format"] = args.audio_format
    if args.bitrate:
        settings["bitrate"] = args.bitrate
    if args.quality:
        settings["quality"] = args.quality
    if args.preset:
        print(describe_settings(settings))

    last = [-1]

    def on_progress(pct, stats):
        if int(pct) != last[0]:
            last[0] = int(pct)
            line = "%5.1f%%  %s" % (pct, format_progress(stats))
            print("\r" + line.ljust(70), end="", flush=True)

    converter = Converter(
        on_log=lambda m: print("\n" + str(m)),
        on_progress=on_progress,
        on_done=lambda ok, m: print("\n" + m),
        browser=args.browser,
        cookie_file=args.cookie_file,
    )
    converter.download(args.url, args.output, settings["mode"], settings["quality"],
                       settings["audio_format"], settings["bitrate"], args.playlist,
                       extras={"subtitles": args.subs,
                               "embed_subs": args.embed_subs,
                               "sponsorblock": args.sponsorblock})


if __name__ == "__main__":
    main()
