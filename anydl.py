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
import tempfile
import subprocess
import urllib.request
from importlib import metadata as importlib_metadata

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(APP_DIR, "bin")

APP_NAME = "anydl"
VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
AUDIO_FORMATS = ["mp3", "m4a", "wav", "opus", "flac"]
AUDIO_BITRATES = ["320", "256", "192", "160", "128", "96"]

PRESETS = [
    {"name": "Music 320", "cli": "music",
     "settings": {"mode": "audio", "audio_format": "mp3", "bitrate": "320"}},
    {"name": "Archive 1080p", "cli": "archive",
     "settings": {"mode": "video", "quality": "1080p"}},
    {"name": "Phone 720p", "cli": "phone",
     "settings": {"mode": "video", "quality": "720p"}},
]
CUSTOM_PRESET = "Custom"

# Profiles yt-dlp can borrow a logged-in session from.
BROWSERS = ["none", "firefox", "chrome", "edge", "brave", "chromium",
            "opera", "vivaldi", "safari", "whale"]

# Only what the window offers; yt-dlp takes any code the site publishes.
SUBTITLE_LANGS = ["none", "en", "pt", "es", "fr", "de", "it", "ja", "ko", "ru", "zh", "all"]

# The rest of SponsorBlock's categories are markers, not stretches of video,
# so there would be nothing to cut.
SPONSOR_CATEGORIES = ["sponsor", "selfpromo", "interaction"]

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def find_preset(name):
    """Look a preset up by its window name or its command line name."""
    for preset in PRESETS:
        if name in (preset["name"], preset["cli"]):
            return preset
    return None


def describe_settings(settings, has_ffmpeg=True):
    """One sentence saying what these choices will produce.

    Derived from the settings rather than written next to each preset, so it
    cannot drift away from what the preset actually does. It also has to know
    whether ffmpeg is there: without it the pipeline quietly does something
    else, and a sentence promising an MP3 while an .m4a lands on disk is worse
    than no sentence at all.
    """
    if settings.get("mode") == "audio":
        audio_format = settings.get("audio_format", "mp3")
        if not has_ffmpeg:
            return ("Audio only: the original track, saved as it comes -- usually .m4a. "
                    "Converting to %s needs ffmpeg." % audio_format.upper())
        tagged = " with the cover art and tags embedded" if audio_format in (
            "mp3", "m4a", "flac") else ""
        if audio_format in ("wav", "flac"):
            return "Audio only: %s, lossless%s." % (audio_format.upper(), tagged)
        return "Audio only: %s at %s kbps%s." % (
            audio_format.upper(), settings.get("bitrate", "192"), tagged)

    quality = settings.get("quality", "Best")
    if not has_ffmpeg:
        # Nothing to merge the picture and the sound with, so we are down to
        # whatever single stream the site already muxed -- those stop near 720p.
        capped = "720p" if quality in ("Best", "2160p", "1440p", "1080p") else quality
        return ("Video: MP4 from one ready-made stream, so about %s. Anything higher "
                "needs ffmpeg to merge the picture and the sound." % capped)
    if quality == "Best":
        return ("Video: MP4 at the highest resolution the site offers, whatever the codec "
                "(so 4K stays possible).")
    return "Video: H.264 MP4 capped at %s, the codec every player and editor reads." % quality


# yt-dlp names its postprocessors after their classes. Nobody downloading a
# song needs to read "FFmpegExtractAudio" to know what is going on.
STAGE_LABELS = {
    "SponsorBlock": "Looking up sponsor segments",
    "ModifyChapters": "Cutting the sponsor segments out",
    "ExtractAudio": "Converting the audio",
    "VideoConvertor": "Converting the video",
    "VideoRemuxer": "Repackaging the video",
    "Merger": "Merging the picture and the sound",
    "Metadata": "Writing the tags",
    "EmbedThumbnail": "Embedding the cover art",
    "EmbedSubtitle": "Putting the subtitles inside the video",
    "SubtitlesConvertor": "Preparing the subtitles",
    "Concat": "Joining the parts",
    "MoveFiles": "Moving the file into place",
    "FixupM3u8": "Repairing the stream",
    "FixupTimestamp": "Repairing the timestamps",
}


def normalise_stage(name):
    """The name a postprocessor is registered under and the name it reports
    are not the same string: yt-dlp takes FFmpegExtractAudio and hands the
    hook back ExtractAudio. Both have to land on the same key."""
    text = str(name or "")
    return text[6:] if text.startswith("FFmpeg") else text


def stage_label(name):
    """A sentence for a postprocessor, falling back to something harmless."""
    return STAGE_LABELS.get(normalise_stage(name), "Processing")


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


def format_size(num_bytes):
    """Bytes as MB or GB, or a question mark when the site does not say."""
    if not num_bytes:
        return "?"
    if num_bytes >= 1073741824:
        return "%.2f GB" % (num_bytes / 1073741824.0)
    return "%.1f MB" % (num_bytes / 1048576.0)


def parse_timestamp(text):
    """'90', '1:30' or '1:02:03' as seconds. Empty gives None; junk raises."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    seconds = 0.0
    for part in parts:
        part = part.strip()
        if not part.replace(".", "", 1).isdigit():
            raise ValueError(text)
        seconds = seconds * 60 + float(part)
    return seconds


def stamp_label(seconds):
    """90 -> 1m30s. No colon: it would be sanitised out of a file name."""
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh%02dm%02ds" % (hours, minutes, secs)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs


def section_suffix(start, end):
    """A tag for the file name, so a clip never lands on top of the full video
    -- yt-dlp would see the name taken and skip the download entirely."""
    if start is not None and end is not None:
        return " [%s-%s]" % (stamp_label(start), stamp_label(end))
    if start is not None:
        return " [from %s]" % stamp_label(start)
    if end is not None:
        return " [to %s]" % stamp_label(end)
    return ""


def format_date(stamp):
    """yt-dlp hands back 20250915; nobody reads a date written like that."""
    text = str(stamp or "")
    if len(text) == 8 and text.isdigit():
        return "%s-%s-%s" % (text[:4], text[4:6], text[6:])
    return text


def format_duration(seconds):
    if not seconds:
        return "?"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def audio_size_estimate(settings, duration):
    """What the converted audio will roughly weigh, since the size yt-dlp
    reports is the source stream and not the MP3 it becomes."""
    if not duration:
        return None
    audio_format = settings.get("audio_format", "mp3")
    if audio_format in ("mp3", "m4a", "opus"):
        try:
            return int(int(settings.get("bitrate", "192")) * 125 * duration)
        except (TypeError, ValueError):
            return None
    if audio_format == "wav":
        return int(44100 * 2 * 2 * duration)  # 16 bit stereo
    return None


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
YTDLP_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"

# Failures that mean a stale yt-dlp rather than a bad link or a private video.
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
    """The interpreter to run pip with: python.exe, never pythonw.exe."""
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
        self.on_stage = on_stage or (lambda stage, before_download: None)
        # Stages yt-dlp runs before the download instead of after it. They must
        # not be mistaken for converting, or a row spends the whole download
        # claiming the download is already over.
        self.pre_download_stages = set()
        self.final_file = None
        self.cancelled = False
        self.rate_limit = None  # bytes per second, or None for as fast as it goes
        self._ydl = None
        self._last_emit = 0.0
        self.ffmpeg_dir = find_ffmpeg()
        self.update_hint = update_hint or UPDATE_HINT
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
        # yt-dlp stamps its own ERROR: on the front. This text now goes on
        # the row and into the details sheet rather than only into a log, so
        # the prefix is noise in front of the sentence that matters.
        while text.upper().startswith("ERROR: "):
            text = text[7:].lstrip()
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
            # The hook fires per chunk; publishing all of them floods the window.
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
            self.on_progress(pct, {"done": done, "total": total,
                                   "speed": speed, "eta": eta})
        elif status == "finished":
            self._last_emit = 0.0
            self.final_file = d.get("filename") or self.final_file
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
        status = d.get("status")
        if status == "finished":
            # A postprocessor renames what it touches -- an .m4a comes out an
            # .mp3 -- so the last one to report is the one that knows the name.
            path = (d.get("info_dict") or {}).get("filepath")
            if path:
                self.final_file = path
            return
        if status != "started":
            return
        if self.cancelled:
            # Stop between stages rather than in the middle of one.
            raise KeyboardInterrupt("cancelled by user")
        stage = str(d.get("postprocessor", ""))
        self.on_log("[convert] " + stage_label(stage))
        self.on_stage(stage, normalise_stage(stage) in self.pre_download_stages)

    # ---------------------------------------------------------------- options
    def _build_options(self, settings):
        out_dir = settings["out_dir"]
        mode = settings.get("mode", "video")
        playlist = settings.get("playlist", False)
        start, end = settings.get("section_start"), settings.get("section_end")
        name = "%(title)s" + section_suffix(start, end) + ".%(ext)s"
        if playlist:
            # The trailing | is the empty default: a link that turns out not to
            # be a playlist would otherwise land in a folder called "NA", and
            # yt-dlp drops the empty path component for us.
            template = os.path.join(out_dir, "%(playlist_title|)s", name)
        else:
            template = os.path.join(out_dir, name)

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
        if self.rate_limit:
            opts["ratelimit"] = self.rate_limit
        if start is not None or end is not None:
            from yt_dlp.utils import download_range_func

            opts["download_ranges"] = download_range_func(
                None, [(start or 0, end if end is not None else float("inf"))])

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
        self.pre_download_stages = {normalise_stage(stage["key"]) for stage in stages
                                    if stage.get("when") == "before_dl"}
        return opts

    def _audio_options(self, opts, stages, audio_format, bitrate):
        if not self.ffmpeg_dir:
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
            # "Best" ignores the codec on purpose: YouTube only serves H.264
            # up to 1080p, so filtering on avc1 here would throw 4K away.
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

        opts["merge_output_format"] = "mp4"

    # ----------------------------------------------------------------- public
    def inspect(self, url, settings):
        """What a download would produce, without downloading it.

        Runs the real format selection against the real settings, so the size
        and the streams reported here are the ones that would be fetched.
        """
        import yt_dlp

        opts = self._build_options(dict(settings, playlist=False))
        opts.update({"noplaylist": True, "playlist_items": "1", "skip_download": True,
                     "progress_hooks": [], "postprocessor_hooks": [], "postprocessors": []})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        playlist = None
        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
            playlist = {"title": info.get("title") or "playlist",
                        "count": info.get("playlist_count") or len(entries)}
            if not entries:
                raise RuntimeError("This playlist has nothing in it.")
            info = entries[0]

        parts = info.get("requested_formats") or [info]
        chosen = {
            "format_id": info.get("format_id"),
            "ext": info.get("ext"),
            "bytes": info.get("filesize") or info.get("filesize_approx"),
            "parts": [self._describe_format(part) for part in parts],
            "clip": None,
        }

        duration = info.get("duration")
        start, end = settings.get("section_start"), settings.get("section_end")
        if duration and (start is not None or end is not None):
            # The size the site reports is for the whole thing; only part of it
            # is going to be fetched.
            span = min(end or duration, duration) - (start or 0)
            if 0 < span < duration:
                share = span / duration
                chosen["clip"] = format_duration(span)
                if chosen["bytes"]:
                    chosen["bytes"] = int(chosen["bytes"] * share)
                for part in chosen["parts"]:
                    if part["bytes"]:
                        part["bytes"] = int(part["bytes"] * share)
        converted = None
        if settings.get("mode") == "audio":
            converted = {"label": describe_settings(settings, bool(self.ffmpeg_dir)),
                         "bytes": audio_size_estimate(settings, info.get("duration"))}

        return {
            "url": url,
            "thumbnail": info.get("thumbnail"),
            "title": info.get("title") or url,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"),
            "upload_date": info.get("upload_date") or "",
            "playlist": playlist,
            "chosen": chosen,
            "converted": converted,
            "formats": [self._describe_format(fmt) for fmt in reversed(info.get("formats") or [])
                        if fmt.get("ext") != "mhtml"],
        }

    @staticmethod
    def _describe_format(fmt):
        video = fmt.get("vcodec") or "none"
        audio = fmt.get("acodec") or "none"
        return {
            "id": fmt.get("format_id") or "",
            "ext": fmt.get("ext") or "",
            "resolution": fmt.get("resolution") or ("audio only" if video == "none" else ""),
            "fps": fmt.get("fps") or "",
            "vcodec": "" if video == "none" else video.split(".")[0],
            "acodec": "" if audio == "none" else audio.split(".")[0],
            "bytes": fmt.get("filesize") or fmt.get("filesize_approx"),
            "note": fmt.get("format_note") or "",
        }

    def run(self, url, settings):
        """Download one link. Returns (outcome, message) and never raises.

        `outcome` is "ok", "partial" or "fail". A playlist runs with
        ignoreerrors, so entries that died come back here as a count rather
        than as an exception; calling that a plain success would present
        silent data loss as a finished job.

        One at a time, each with its own YoutubeDL, because every queue item
        carries its own settings and a failure must stop that item alone.
        """
        try:
            import yt_dlp
        except ImportError:
            return "fail", "yt-dlp is not installed. Run: python -m pip install -U yt-dlp"
        if self.cancelled:
            return "fail", "Cancelled."

        out_dir = settings["out_dir"]
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            return "fail", "Cannot use the folder %s\n%s" % (out_dir, exc)
        self._last_emit = 0.0
        self.final_file = None
        try:
            with yt_dlp.YoutubeDL(self._build_options(settings)) as ydl:
                # Held so the speed limit can be moved while this is running:
                # the downloader reads that parameter on every tick.
                self._ydl = ydl
                failed = ydl.download([url])
        except KeyboardInterrupt:
            return "fail", "Cancelled."
        except Exception as exc:  # noqa: BLE001
            return "fail", self._explain(exc)
        finally:
            self._ydl = None

        if failed:
            self.on_log("[warning] some entries could not be downloaded; see the errors above.")
            return "partial", "%d entr%s could not be downloaded -- the rest are in %s" % (
                failed, "y" if failed == 1 else "ies", out_dir)
        if self.final_file:
            return "ok", "Saved " + os.path.basename(self.final_file)
        return "ok", "Saved to " + out_dir

    def set_rate_limit(self, bytes_per_second):
        """Change the speed cap, including on a download already in flight."""
        self.rate_limit = bytes_per_second or None
        ydl = self._ydl
        if ydl is not None:
            ydl.params["ratelimit"] = self.rate_limit

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
            outcome, message = self.run(url, settings)
            if outcome == "ok":
                continue
            if outcome == "partial":
                failures += 1
                self.on_log("[warning] " + message)
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
tk = ttk = filedialog = messagebox = tkfont = None

# ------------------------------------------------------------------- tokens
# Every gap, colour and font the window uses is named here, so a change lands
# in one place instead of in thirty call sites.
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

# The surface the widgets sit on belongs to the platform theme, so the text on
# it cannot be a constant: the grey that reads on white fails on the darker
# background Tk's X11 default hands out. These are picked to clear 4.5:1
# against the palest and the darkest surface we can be given.
LIGHT_PALETTE = {
    "field": "#ffffff",
    "border": "#b0b0b0",
    "muted": "#595959",
    "accent": "#0a5fb4",
    "success": "#1a7f37",
    "warning": "#8a5b00",
    "error": "#b42318",
    "warn_bg": "#fdf3c9",
    "on_warn": "#4a3a00",
    "sunken": "#1b1f23",
    "on_sunken": "#c9d1d9",
}
DARK_PALETTE = dict(
    LIGHT_PALETTE, field="#1e1e1e", border="#555555", muted="#a8a8a8",
    accent="#4c9aff", success="#3fb950", warning="#d29922", error="#f85149",
    warn_bg="#3a2e00", on_warn="#f0e0a0",
)

# One row per queue status: the word the row shows, the colour it takes,
# whether it has stopped for good, whether it is still moving, and where it
# sorts in the summary line. QueueItem, refresh_row and refresh_status all
# read this rather than each keeping its own copy of the same list.
STATUS_TABLE = {
    "Queued":      {"label": "Queued",      "tone": "muted",   "over": False, "busy": False, "rank": 5},
    "Starting":    {"label": "Starting",    "tone": "accent",  "over": False, "busy": True,  "rank": 1},
    "Downloading": {"label": "Downloading", "tone": "accent",  "over": False, "busy": True,  "rank": 0},
    "Retrying":    {"label": "Retrying",    "tone": "warning", "over": False, "busy": True,  "rank": 2},
    "Converting":  {"label": "Processing",  "tone": "accent",  "over": False, "busy": True,  "rank": 3},
    "Pausing":     {"label": "Pausing",     "tone": "muted",   "over": False, "busy": True,  "rank": 4},
    "Cancelling":  {"label": "Cancelling",  "tone": "muted",   "over": False, "busy": True,  "rank": 4},
    "Paused":      {"label": "Paused",      "tone": "warning", "over": False, "busy": False, "rank": 6},
    "Done":        {"label": "Done",        "tone": "success", "over": True,  "busy": False, "rank": 7},
    "Partial":     {"label": "Partial",     "tone": "warning", "over": True,  "busy": False, "rank": 8},
    "Failed":      {"label": "Failed",      "tone": "error",   "over": True,  "busy": False, "rank": 9},
    "Cancelled":   {"label": "Cancelled",   "tone": "muted",   "over": True,  "busy": False, "rank": 10},
}
STATUS_ORDER = sorted(STATUS_TABLE, key=lambda name: STATUS_TABLE[name]["rank"])

# A row that has not reported anything for this long is stuck in one of
# yt-dlp's silent retries, which run with warnings off and would otherwise
# look exactly like a frozen application.
STALL_SECONDS = 12.0

EMPTY_ROW = "placeholder"  # iid of the "nothing here yet" line in the queue


def _load_tk():
    global tk, ttk, filedialog, messagebox, tkfont
    import tkinter
    from tkinter import ttk as ttk_module
    from tkinter import filedialog as filedialog_module
    from tkinter import messagebox as messagebox_module
    from tkinter import font as font_module

    tk = tkinter
    ttk = ttk_module
    filedialog = filedialog_module
    messagebox = messagebox_module
    tkfont = font_module


class Theme:
    """Colours, gaps and fonts, resolved once against the running platform.

    Two things here cannot be decided ahead of time. The surface colour is
    whatever the ttk theme supplies, so the text ramp has to be chosen after
    reading it. And every gap in the file is a pixel count while fonts are
    points, so on a scaled display the text grows and the spacing does not --
    px() puts them back on the same footing.
    """

    def __init__(self, window):
        self.style = ttk.Style(window)
        if sys.platform.startswith("linux") and "clam" in self.style.theme_names():
            # Tk's X11 default is the Motif-era theme, which is both dated and
            # the one theme of the three that cannot be styled at all.
            self.style.theme_use("clam")
        self.scale = max(1.0, window.winfo_fpixels("1i") / 96.0)
        self.surface = self.style.lookup("TFrame", "background") or "#f0f0f0"
        self.palette = DARK_PALETTE if self._is_dark(window) else LIGHT_PALETTE
        self._make_fonts(window)
        self._make_styles(window)

    # ------------------------------------------------------------- accessors
    def c(self, role):
        return self.palette[role]

    def px(self, name):
        return int(round(SPACE[name] * self.scale))

    def pad(self, *names):
        return tuple(self.px(name) for name in names)

    # --------------------------------------------------------------- private
    def _is_dark(self, window):
        try:
            red, green, blue = window.winfo_rgb(self.surface)
        except Exception:  # noqa: BLE001
            return False
        return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 65535.0 < 0.5

    def _make_fonts(self, window):
        base = tkfont.nametofont("TkDefaultFont")
        size = base.cget("size") or 9
        family = base.cget("family")

        def make(name, delta=0, weight="normal", face=None):
            try:
                tkfont.Font(window, name=name, exists=False, family=face or family,
                            size=size + delta, weight=weight)
            except Exception:  # noqa: BLE001
                tkfont.nametofont(name).configure(family=face or family,
                                                  size=size + delta, weight=weight)
            return name

        self.title = make("AnydlTitle", 4, "bold")
        self.section = make("AnydlSection", 0, "bold")
        self.body = make("AnydlBody", 0)
        self.action = make("AnydlAction", 1, "bold")
        # One step down is the floor: below this Segoe UI stops being readable
        # at 100% scaling.
        self.small = make("AnydlSmall", -1)
        # TkFixedFont is pinned to Courier New 10 on Windows, so leaning on it
        # would make the queue the one region that ignores the OS text size.
        self.mono = make("AnydlMono", 1, face=tkfont.nametofont("TkFixedFont").cget("family"))
        self.mono_size = size + 1

    def _make_styles(self, window):
        style, colour = self.style, self.c
        style.configure("TLabel", font=self.body)
        style.configure("Muted.TLabel", font=self.small, foreground=colour("muted"))
        style.configure("Section.TLabel", font=self.section)
        style.configure("Title.TLabel", font=self.title, foreground=colour("accent"))
        style.configure("Head.TLabel", font=self.title)
        style.configure("Error.TLabel", font=self.body, foreground=colour("error"))
        style.configure("Warning.TLabel", font=self.body, foreground=colour("warning"))
        style.configure("Success.TLabel", font=self.body, foreground=colour("success"))
        style.configure("Banner.TFrame", background=colour("warn_bg"))
        style.configure("Banner.TLabel", background=colour("warn_bg"),
                        foreground=colour("on_warn"), font=self.body)
        # -background on a button is discarded by the Windows and macOS theme
        # engines, so the primary action has to earn its weight from type size
        # and padding instead of from a fill.
        style.configure("Primary.TButton", font=self.action,
                        padding=(self.px("lg"), self.px("sm")))
        style.configure("Link.TButton", font=self.small)
        style.configure("Treeview.Heading", font=self.section)
        style.configure("Queue.Treeview", font=self.mono,
                        rowheight=tkfont.Font(font=self.mono).metrics("linespace")
                        + self.px("xs"))
        # A tag foreground drawn over the selection blue is unreadable, so the
        # theme's own selected pair has to stay in the map.
        selected = self._selected_foreground()
        keep = [entry for entry in style.map("Treeview", "foreground")
                if "selected" not in str(entry[0])]
        style.map("Treeview", foreground=keep + [("selected", selected)])
        # The combobox dropdown is a classic Tk listbox and is not themed; left
        # alone it stays grey while everything around it changes.
        window.option_add("*TCombobox*Listbox.font", self.body)

    def _selected_foreground(self):
        for entry in self.style.map("Treeview", "foreground"):
            if "selected" in str(entry[0]):
                return entry[1]
        return "SystemHighlightText" if sys.platform == "win32" else "#ffffff"


class QueueItem:
    """One line of the queue, holding the settings as they were when it was
    added: a queue can mix an MP3 and a 1080p video, and changing a dropdown
    afterwards does not rewrite what is already waiting."""

    def __init__(self, uid, url, settings, label=None):
        self.uid = uid
        self.url = url
        self.settings = settings
        self.label = label or url  # replaced by the real title once known
        self.status = "Queued"
        self.progress = 0.0
        self.detail = ""
        self.note = ""  # the whole failure text, not just the first line
        self.cancelled = False
        self.pause_requested = False
        self.converter = None
        self.last_event = time.monotonic()

    @property
    def finished(self):
        return STATUS_TABLE[self.status]["over"]

    @property
    def busy(self):
        return STATUS_TABLE[self.status]["busy"]

    @property
    def summary(self):
        """The settings this row carries, short enough for a column.

        Two rows in the same queue can be doing entirely different things, and
        until this existed there was no way to tell them apart.
        """
        settings = self.settings
        if settings.get("mode") == "audio":
            parts = [settings.get("audio_format", "mp3").upper(),
                     settings.get("bitrate", "192")]
        else:
            parts = ["MP4", settings.get("quality", "Best")]
        if settings.get("playlist"):
            parts.append("playlist")
        if settings.get("subtitles", "none") != "none":
            parts.append("sub " + settings["subtitles"])
        if settings.get("sponsorblock"):
            parts.append("no sponsors")
        if settings.get("section_start") is not None or settings.get("section_end") is not None:
            parts.append("clip")
        return " · ".join(parts)


def progress_bar(pct, cells=16):
    """A bar drawn in text: a Treeview cell cannot hold a widget.

    Sixteen cells rather than ten. Ten meant a 2 GB file only moved every
    200 MB and read as frozen; going much past sixteen starts dictating how
    wide the whole window has to be.
    """
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * cells))
    return "█" * filled + "░" * (cells - filled)


class AnydlApp:
    """The window.

    Downloads run on worker threads, and a worker must never touch a Tk widget.
    They publish onto `self.events`, and `pump()` drains it on the main thread.
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
        self.rate_limit = None
        self.last_clipboard = ""
        self.applying_preset = False
        self.checking = False
        self.installed_version = None
        self.latest_version = None
        # None until the check comes back; False only once we know it is gone.
        self.engine_ok = None
        self.extras_open = False
        self.bar_mode = "determinate"
        self.ready = False  # set once the window has been sized
        self.base_title = APP_NAME + " - video and audio downloader"

        self.window = tk.Tk()
        self.window.title(self.base_title)
        self.window.minsize(700, 500)  # replaced by _fit_to_screen once built
        apply_window_icon(self.window)
        self.theme = Theme(self.window)

        # The banner is built first so keyboard focus reaches it first: Tk
        # traverses in creation order, and pack(before=...) moves the pixels
        # without moving the tab stop. It carries the one message that can
        # explain why nothing else in the window works.
        self._build_banner(self.window)

        self.top = ttk.Frame(self.window, padding=self.theme.px("md"))
        self.top.pack(fill="x")
        self._build_links(self.top)
        self._build_message(self.top)
        ttk.Separator(self.top).pack(fill="x", pady=self.theme.pad("md", "md"))
        self._build_presets(self.top)
        self._build_choices(self.top)
        self._build_disclosure(self.top)
        self._build_extras(self.top)
        ttk.Separator(self.top).pack(fill="x", pady=self.theme.pad("md", "md"))
        self._build_destination(self.top)
        self._build_buttons(self.top)

        # Creation order is tab order and pack order is layout; the two are
        # independent. The body is built first so it is reached before the
        # footer, and packed last so the footer keeps its strip at the bottom.
        body = ttk.Frame(self.window, padding=(self.theme.px("md"), 0,
                                               self.theme.px("md"), self.theme.px("md")))
        self._build_progress(body)
        split = ttk.PanedWindow(body, orient="vertical")
        self._build_queue(split)
        self._build_log(split)
        self._build_footer(self.window)
        body.pack(fill="both", expand=True)
        split.pack(fill="both", expand=True, pady=self.theme.pad("sm", "xs"))

        self.setting_vars = {
            "mode": self.mode_var,
            "quality": self.quality_var,
            "audio_format": self.format_var,
            "bitrate": self.bitrate_var,
        }
        for variable in self.setting_vars.values():
            variable.trace_add("write", self.on_manual_change)
        self.mode_var.trace_add("write", self.sync_fields)
        for variable in (self.playlist_var, self.subs_var, self.sponsor_var,
                         self.section_start_var, self.section_end_var,
                         self.browser_var, self.cookie_file_var):
            variable.trace_add("write", self.refresh_disclosure)
        self.sync_fields()
        self.describe_preset()
        self.show_empty_row()
        self._bind_keys()
        # Shut first, then measure: the minimum has to describe how the window
        # actually opens. Opening the panel later grows the window instead of
        # having reserved its height from the start.
        self.toggle_extras()
        self._fit_to_screen()
        self.ready = True

        self.urls_box.focus_set()
        if self.ffmpeg_dir:
            self.log("ffmpeg detected.")
        else:
            self.log("[warning] ffmpeg not found: MP3 output and 1080p+ are unavailable. "
                     "See the README for how to install it.")
            self.show_banner(
                "ffmpeg is not installed. Audio is saved as it comes instead of converted, "
                "video stops near 720p, and subtitles and SponsorBlock are unavailable.",
                None)

    def _fit_to_screen(self):
        """Size the window from what the widgets need, not from the screen.

        Everything above the queue has a fixed height, so a short window
        squeezes the queue and the log -- to nothing at all, in the worst case.
        The required size becomes the minimum; a taller screen buys them more
        room. (Under WSLg the reported screen is 640x480, which is how the two
        panes came to vanish entirely.)
        """
        self.window.update_idletasks()
        needed = self.window.winfo_reqheight()
        # Width has to come from the widgets too: at a larger system font the
        # button row outgrows any constant, and pack answers that by quietly
        # dropping whatever it cannot fit.
        wide = max(700, self.window.winfo_reqwidth())
        # Never demand more height than the screen has. The queue and the log
        # can give ground -- they sit in a paned window for exactly that
        # reason -- but a minimum taller than the display is a window that
        # cannot be opened at all.
        ceiling = max(480, self.window.winfo_screenheight() - 160)
        self.window.minsize(wide, min(needed, ceiling))
        # Opening size, and where to put it. Tk's own placement had the
        # window starting a third of the way down a 1080px screen at 960px
        # tall, which put the footer under the bottom edge.
        screen_h = self.window.winfo_screenheight()
        screen_w = self.window.winfo_screenwidth()
        high = max(min(needed, ceiling), min(screen_h - 200, needed + 120))
        wide = min(wide + 40, screen_w - 80)
        self.window.geometry("%dx%d+%d+%d" % (
            wide, high, max(0, (screen_w - wide) // 2), max(0, (screen_h - high) // 3)))

    def run(self):
        self.pump()
        self.poll_clipboard()
        self.check_for_updates()  # in the background; the window is already up
        self.window.mainloop()

    # ------------------------------------------------------------------ build
    def _build_banner(self, parent):
        """A strip that stays hidden until something needs attention."""
        self.banner = ttk.Frame(parent, style="Banner.TFrame")
        inner = ttk.Frame(self.banner, style="Banner.TFrame",
                          padding=self.theme.pad("md", "sm", "md", "sm"))
        inner.pack(fill="x")

        self.banner_var = tk.StringVar(value="")
        ttk.Label(inner, textvariable=self.banner_var, style="Banner.TLabel",
                  anchor="w", justify="left", wraplength=520).pack(side="left",
                                                                   fill="x", expand=True)
        # Built before the dismiss button so the tab order matches what the eye
        # sees: packing to the right puts the first one packed furthest right.
        self.update_btn = ttk.Button(inner, text="Update now", command=self.start_update)
        self.dismiss_btn = ttk.Button(inner, text="Later", command=self.hide_banner)
        self.update_btn.pack(side="right", padx=self.theme.pad("sm", "sm"))
        self.dismiss_btn.pack(side="right")

    def _build_footer(self, parent):
        footer = ttk.Frame(parent, padding=(self.theme.px("md"), 0,
                                            self.theme.px("md"), self.theme.px("sm")))
        footer.pack(fill="x", side="bottom")
        self.footer_var = tk.StringVar(value="checking yt-dlp...")
        ttk.Label(footer, textvariable=self.footer_var,
                  style="Muted.TLabel").pack(side="left")
        self.check_btn = ttk.Button(footer, text="Check for updates", style="Link.TButton",
                                    command=lambda: self.check_for_updates(announce=True))
        self.check_btn.pack(side="right")

    def _build_links(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left",
                                                                    padx=(0, self.theme.px("md")))
        ttk.Label(header, text="Paste a link, or several, one per line",
                  style="Section.TLabel").pack(side="left")
        self.watch_clipboard_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="Watch the clipboard",
                        variable=self.watch_clipboard_var,
                        command=self.on_watch_toggled).pack(side="right")

        # A classic Tk Text is the only multi-line field there is, so its
        # border and its focus ring have to be drawn by hand. Before this the
        # primary input of the application had a fainter focus indicator than
        # the two small entries that hold a timestamp.
        self.urls_box = tk.Text(parent, height=4, wrap="char", relief="flat",
                                borderwidth=0, highlightthickness=2,
                                highlightbackground=self.theme.c("border"),
                                highlightcolor=self.theme.c("accent"),
                                background=self.theme.c("field"),
                                font=self.theme.body,
                                padx=self.theme.px("sm"), pady=self.theme.px("xs"))
        self.urls_box.pack(fill="x", pady=self.theme.pad("sm", "xs"))

    def _build_message(self, parent):
        """One line under the box for anything that used to open a dialog.

        A native message box runs its own modal loop, which stops Tk's timers,
        which stops pump() -- so telling somebody they forgot a link would
        freeze the download that was already running.
        """
        self.message_var = tk.StringVar(value="")
        self.message = ttk.Label(parent, textvariable=self.message_var,
                                 style="Error.TLabel", justify="left", wraplength=680)

    def _build_presets(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="Preset:").pack(side="left")

        self.preset_var = tk.StringVar(value=CUSTOM_PRESET)
        for preset in PRESETS:
            button = ttk.Radiobutton(row, text=preset["name"], value=preset["name"],
                                     variable=self.preset_var, command=self.apply_preset)
            button.pack(side="left", padx=(self.theme.px("md"), 0))
            # Focus as well as hover: arrowing through the group used to show
            # nothing, so the only way to find out what a preset did was to
            # pick it and have it rewrite your settings.
            button.bind("<Enter>", lambda _e, chosen=preset: self.preview_preset(chosen))
            button.bind("<FocusIn>", lambda _e, chosen=preset: self.preview_preset(chosen))
            button.bind("<Leave>", lambda _e: self.describe_preset())
            button.bind("<FocusOut>", lambda _e: self.describe_preset())
        ttk.Radiobutton(row, text=CUSTOM_PRESET, value=CUSTOM_PRESET,
                        variable=self.preset_var,
                        command=self.describe_preset).pack(side="left",
                                                           padx=(self.theme.px("md"), 0))

    def _build_choices(self, parent):
        choices = ttk.Frame(parent)
        choices.pack(fill="x", pady=self.theme.pad("md", "xs"))

        self.mode_var = tk.StringVar(value="video")
        ttk.Radiobutton(choices, text="Video (MP4)", value="video",
                        variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(choices, text="Audio only", value="audio",
                        variable=self.mode_var).pack(side="left",
                                                     padx=(self.theme.px("lg"), 0))

        self.quality_label = ttk.Label(choices, text="Quality:")
        self.quality_label.pack(side="left", padx=(self.theme.px("xl"), 0))
        self.quality_var = tk.StringVar(value="Best")
        self.quality_box = ttk.Combobox(choices, values=VIDEO_QUALITIES, width=10,
                                        state="readonly", textvariable=self.quality_var)
        self.quality_box.pack(side="left", padx=(self.theme.px("sm"), 0))

        self.format_label = ttk.Label(choices, text="Audio format:")
        self.format_label.pack(side="left", padx=(self.theme.px("xl"), 0))
        self.format_var = tk.StringVar(value="mp3")
        self.format_box = ttk.Combobox(choices, values=AUDIO_FORMATS, width=8,
                                       state="readonly", textvariable=self.format_var)
        self.format_box.pack(side="left", padx=(self.theme.px("sm"), 0))

        self.bitrate_label = ttk.Label(choices, text="Bitrate:")
        self.bitrate_label.pack(side="left", padx=(self.theme.px("lg"), 0))
        self.bitrate_var = tk.StringVar(value="192")
        self.bitrate_box = ttk.Combobox(choices, values=AUDIO_BITRATES, width=6,
                                        state="readonly", textvariable=self.bitrate_var)
        self.bitrate_box.pack(side="left", padx=(self.theme.px("sm"), 0))

        # The sentence saying what all of the above will actually produce. It
        # is the most useful line in the window and used to be styled as the
        # least important thing on screen.
        self.preset_note = ttk.Label(parent, text="", justify="left", wraplength=680)
        self.preset_note.pack(anchor="w")
        self.gate_note = ttk.Label(parent, text="", style="Muted.TLabel",
                                   justify="left", wraplength=680)
        self.gate_note.pack(anchor="w", pady=self.theme.pad("xs", "xs"))

    def _build_disclosure(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        self.disclosure_var = tk.BooleanVar(value=False)
        self.disclosure = ttk.Checkbutton(row, style="Toolbutton",
                                          text="▸  More options",
                                          variable=self.disclosure_var,
                                          command=self.toggle_extras)
        self.disclosure.pack(side="left")
        # Folded is not the same as hidden: if anything in there is switched on
        # it has to say so from out here.
        self.disclosure_note = ttk.Label(row, text="", style="Muted.TLabel")
        self.disclosure_note.pack(side="left", padx=(self.theme.px("sm"), 0))

    def _build_extras(self, parent):
        self.extras = ttk.Frame(parent)
        self.extras.pack(fill="x")

        row = ttk.Frame(self.extras)
        row.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        self.playlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Download the whole playlist",
                        variable=self.playlist_var).pack(side="left")

        self.subs_label = ttk.Label(row, text="Subtitles:")
        self.subs_label.pack(side="left", padx=(self.theme.px("xl"), 0))
        self.subs_var = tk.StringVar(value="none")
        self.subs_box = ttk.Combobox(row, values=SUBTITLE_LANGS, width=6,
                                     state="readonly", textvariable=self.subs_var)
        self.subs_box.pack(side="left", padx=self.theme.pad("sm", "sm"))
        self.subs_var.trace_add("write", self.sync_fields)

        self.embed_subs_var = tk.BooleanVar(value=True)
        self.embed_subs_check = ttk.Checkbutton(row, text="inside the video, not a .srt file",
                                                variable=self.embed_subs_var)
        self.embed_subs_check.pack(side="left")

        self.sponsor_var = tk.BooleanVar(value=False)
        self.sponsor_check = ttk.Checkbutton(row, text="Cut sponsor segments (SponsorBlock)",
                                             variable=self.sponsor_var)
        self.sponsor_check.pack(side="left", padx=(self.theme.px("xl"), 0))

        clip = ttk.Frame(self.extras)
        clip.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        ttk.Label(clip, text="Only from:").pack(side="left")
        self.section_start_var = tk.StringVar(value="")
        self.section_start = ttk.Entry(clip, textvariable=self.section_start_var, width=9)
        self.section_start.pack(side="left", padx=self.theme.pad("sm", "sm"))
        ttk.Label(clip, text="to:").pack(side="left")
        self.section_end_var = tk.StringVar(value="")
        self.section_end = ttk.Entry(clip, textvariable=self.section_end_var, width=9)
        self.section_end.pack(side="left", padx=self.theme.pad("sm", "sm"))
        ttk.Label(clip, text="leave empty for the whole thing; 1:30 or 90 or 1:02:03",
                  style="Muted.TLabel").pack(side="left")

        login = ttk.Frame(self.extras)
        login.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        ttk.Label(login, text="Sign-in cookies:").pack(side="left")
        self.browser_var = tk.StringVar(value="none")
        # safari and whale on a Windows machine are noise, and picking one is
        # a failure that only announces itself after the download starts.
        browsers = [name for name in BROWSERS
                    if not (name == "safari" and sys.platform != "darwin")]
        ttk.Combobox(login, values=browsers, width=10, state="readonly",
                     textvariable=self.browser_var).pack(side="left",
                                                         padx=self.theme.pad("sm", "sm"))
        self.cookie_file_var = tk.StringVar(value="")
        ttk.Button(login, text="cookies.txt...",
                   command=self.pick_cookie_file).pack(side="left")
        # What was chosen, and a way back out of it. Before this the only trace
        # of a selected file was one line in the log.
        self.cookie_name = ttk.Label(login, text="", style="Muted.TLabel")
        self.cookie_name.pack(side="left", padx=(self.theme.px("sm"), 0))
        self.cookie_clear = ttk.Button(login, text="clear", style="Link.TButton",
                                       command=self.clear_cookie_file)
        self.browser_note = ttk.Label(self.extras, text="", style="Muted.TLabel",
                                      justify="left", wraplength=680)
        self.browser_note.pack(anchor="w")
        self.browser_var.trace_add("write", self.sync_login)

    def _build_destination(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="Save to:").pack(side="left")
        self.dest_var = tk.StringVar(value=self.fallback_dir)
        ttk.Entry(row, textvariable=self.dest_var).pack(side="left", fill="x",
                                                        expand=True,
                                                        padx=self.theme.pad("sm", "sm"))
        ttk.Button(row, text="Browse...", command=self.choose_folder).pack(side="left")
        ttk.Button(row, text="Open folder",
                   command=self.open_folder).pack(side="left", padx=(self.theme.px("sm"), 0))

    def _build_buttons(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=self.theme.pad("md", "xs"))
        self.download_btn = ttk.Button(row, text="Download", style="Primary.TButton",
                                       command=self.start)
        self.download_btn.pack(side="left")
        self.preview_btn = ttk.Button(row, text="Preview", command=self.preview)
        self.preview_btn.pack(side="left", padx=(self.theme.px("sm"), 0))
        ttk.Label(row, text="Ctrl+Enter", style="Muted.TLabel").pack(
            side="left", padx=(self.theme.px("md"), 0))

    def _build_progress(self, parent):
        self.bar = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=self.theme.pad("sm", "xs"))

        row = ttk.Frame(parent)
        row.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(row, textvariable=self.status_var).pack(side="left")
        # Speed, ETA and the byte count, which used to be the first characters
        # the queue's fixed-width column threw away.
        self.active_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.active_var,
                  style="Muted.TLabel").pack(side="right")

        self.preview_var = tk.StringVar(value="")
        # Its own line: sharing one StringVar with the queue meant any queue
        # event wiped "Reading ..." within a tenth of a second.
        self.preview_line = ttk.Label(parent, textvariable=self.preview_var,
                                      style="Muted.TLabel")

    def _build_queue(self, parent):
        frame = ttk.Frame(parent)
        parent.add(frame, weight=4)

        # The toolbar goes down first: a pane dragged small has to lose rows,
        # not the buttons that act on them.
        self._build_queue_tools(frame)

        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        self.tree = ttk.Treeview(frame, columns=("settings", "status", "progress"),
                                 show="tree headings", height=6,
                                 selectmode="extended", yscrollcommand=scrollbar.set)
        # Headings left-aligned, like the cells under them.
        self.tree.heading("#0", text="Link", anchor="w")
        self.tree.heading("settings", text="Settings", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.heading("progress", text="Progress", anchor="w")
        self.tree.column("#0", width=240, minwidth=140, stretch=True)
        self.tree.column("settings", width=165, minwidth=90, stretch=False, anchor="w")
        self.tree.column("status", width=90, minwidth=70, stretch=False, anchor="w")
        # Measured rather than guessed, so the bar, the percentage and the
        # speed beside them fit. The longer sentences a postprocessor produces
        # are shown without a bar in front of them, so they are not the case
        # this has to size for.
        widest = tkfont.Font(font=self.theme.mono).measure(
            progress_bar(100) + " 100.0%  12.34 MB/s  ETA 3600s")
        self.tree.column("progress", width=widest, minwidth=180, stretch=True, anchor="w")
        self.tree.configure(style="Queue.Treeview")
        for name, spec in STATUS_TABLE.items():
            self.tree.tag_configure(name, foreground=self.theme.c(spec["tone"]))
        self.tree.tag_configure(EMPTY_ROW, foreground=self.theme.c("muted"))
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.tree.yview)

        self.tree.bind("<Double-1>", self.open_item_folder)
        self.tree.bind("<Delete>", lambda _event: self.clear_selected())
        # The key labelled Delete on a Mac keyboard sends BackSpace.
        self.tree.bind("<BackSpace>", lambda _event: self.clear_selected())
        self.tree.bind("<Return>", self.open_item_folder)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.refresh_queue_tools())

        self.row_menu = tk.Menu(self.tree, tearoff=0, postcommand=self.sync_row_menu)
        self.row_menu.add_command(label="Retry", command=self.retry_selected)
        self.row_menu.add_command(
            label="Pause", command=lambda: [self.pause_item(i) for i in self.selected_items()])
        self.row_menu.add_command(
            label="Resume", command=lambda: [self.resume_item(i) for i in self.selected_items()])
        self.row_menu.add_separator()
        self.row_menu.add_command(label="Copy link", command=self.copy_selected_link)
        self.row_menu.add_command(label="Why did it fail?", command=self.show_row_detail)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="Cancel", command=self.cancel_selected)
        self.row_menu.add_command(label="Remove", command=self.clear_selected)
        self.row_menu.add_command(label="Open folder", command=self.open_item_folder)
        self.tree.bind("<Button-3>", self.show_row_menu)
        self.tree.bind("<Button-2>", self.show_row_menu)  # right button on a Mac trackpad
        # Everything above was mouse-only, and the per-item pause, resume and
        # retry live nowhere else.
        self.tree.bind("<Shift-F10>", self.show_row_menu)
        self.tree.bind("<Key-App>", self.show_row_menu)

    def _build_queue_tools(self, parent):
        row = ttk.Frame(parent, padding=(0, self.theme.px("xs"), 0, 0))
        row.pack(side="bottom", fill="x")
        self.pause_btn = ttk.Button(row, text="Pause", state="disabled",
                                    command=self.on_pause_pressed)
        self.pause_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row, text="Cancel selected", state="disabled",
                                     command=self.cancel_selected)
        self.cancel_btn.pack(side="left", padx=(self.theme.px("sm"), 0))
        self.cancel_all_btn = ttk.Button(row, text="Cancel all", state="disabled",
                                         command=self.cancel_all)
        self.cancel_all_btn.pack(side="left", padx=(self.theme.px("sm"), 0))
        self.clear_btn = ttk.Button(row, text="Clear finished", state="disabled",
                                    command=self.clear_finished)
        self.clear_btn.pack(side="left", padx=(self.theme.px("sm"), 0))

        # A limit that applies to the download in flight belongs with the
        # queue, not in the status area it used to crowd out.
        self.rate_text = tk.StringVar(value="no limit")
        ttk.Label(row, textvariable=self.rate_text, style="Muted.TLabel",
                  width=10, anchor="e").pack(side="right")
        self.rate_var = tk.DoubleVar(value=0.0)
        ttk.Scale(row, from_=0.0, to=10.0, variable=self.rate_var, length=140,
                  command=self.on_rate_changed).pack(side="right",
                                                     padx=self.theme.pad("sm", "sm"))
        ttk.Label(row, text="Speed limit:", style="Muted.TLabel").pack(side="right")

    def _build_log(self, parent):
        frame = ttk.Frame(parent)
        # One against the queue's four: this is a debug console, and it used to
        # be both the largest and the highest-contrast object in the window.
        parent.add(frame, weight=1)

        header = ttk.Frame(frame)
        header.pack(side="top", fill="x", pady=self.theme.pad("xs", "xs"))
        ttk.Label(header, text="Log", style="Section.TLabel").pack(side="left")
        ttk.Button(header, text="Clear", style="Link.TButton",
                   command=self.clear_log).pack(side="right")
        ttk.Button(header, text="Copy", style="Link.TButton",
                   command=self.copy_log).pack(side="right",
                                               padx=(0, self.theme.px("sm")))

        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        self.log_box = tk.Text(frame, height=4, wrap="word", state="disabled",
                               relief="flat", borderwidth=0,
                               background=self.theme.c("sunken"),
                               foreground=self.theme.c("on_sunken"),
                               insertbackground=self.theme.c("on_sunken"),
                               font=self.theme.mono,
                               yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.log_box.yview)
        for role in ("error", "warning", "success", "muted"):
            self.log_box.tag_configure(role, foreground=self.theme.c(role))

    def _bind_keys(self):
        """What the window answers to. All of this was missing.

        A Text swallows Tab -- it inserts one and stops the traversal chain --
        so the app's primary input was a trap two stops from a fresh window.
        """
        self.urls_box.bind("<Tab>", self._focus_next)
        self.urls_box.bind("<Shift-Tab>", self._focus_prev)
        self.urls_box.bind("<Control-Return>", self._start_from_key)
        self.window.bind("<Control-Return>", self._start_from_key)
        self.window.bind("<Return>", self._return_pressed)
        self.window.bind("<Escape>", self._escape_pressed)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

    def _focus_next(self, _event):
        self.urls_box.tk_focusNext().focus_set()
        return "break"

    def _focus_prev(self, _event):
        self.urls_box.tk_focusPrev().focus_set()
        return "break"

    def _start_from_key(self, _event=None):
        self.start()
        return "break"

    def _return_pressed(self, event):
        # Enter inside a multi-line box is a newline; anywhere else it is the
        # button the window would have marked as default if ttk had one.
        if isinstance(event.widget, tk.Text) or event.widget is self.tree:
            return None
        self.start()
        return "break"

    def _escape_pressed(self, _event):
        if self.banner.winfo_ismapped():
            self.hide_banner()
        return None

    # ---------------------------------------------------------------- widgets
    def log(self, text, role=None):
        if role is None:
            lowered = text.lstrip().lower()
            if lowered.startswith("[error]"):
                role = "error"
            elif lowered.startswith("[warning]"):
                role = "warning"
            elif lowered.startswith(("[update]", "[clipboard]", "[cookies]", "[cancelled]",
                                     "[convert]")):
                role = "muted"
        self.log_box.configure(state="normal")
        start = self.log_box.index("end-1c")
        self.log_box.insert("end", text + "\n")
        if role:
            self.log_box.tag_add(role, start, "end-1c")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def copy_log(self):
        """The log holds every full error, and it could not be selected."""
        text = self.log_box.get("1.0", "end-1c")
        if not text.strip():
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(text)
        self.say("The log is on the clipboard.", "muted")

    def say(self, text, tone="error"):
        """The inline replacement for a modal dialog."""
        self.message_var.set(text)
        self.message.configure(style={"error": "Error.TLabel",
                                      "muted": "Muted.TLabel",
                                      "warning": "Warning.TLabel"}.get(tone, "Error.TLabel"))
        if not self.message.winfo_ismapped():
            self.message.pack(anchor="w", fill="x", pady=(0, self.theme.px("xs")))

    def hush(self):
        if self.message.winfo_ismapped():
            self.message.pack_forget()

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
        self.preset_note.configure(
            text=describe_settings(settings, bool(self.ffmpeg_dir)))

    def preview_preset(self, preset):
        self.preset_note.configure(
            text=describe_settings(dict(self.current_settings(), **preset["settings"]),
                                   bool(self.ffmpeg_dir)))

    def sync_fields(self, *_args):
        """Which controls apply right now, and a sentence saying why.

        A greyed control with no reason beside it reads as a bug. Worse, ttk
        drops disabled widgets out of the tab order, so the route through the
        form silently changes shape when the mode does.
        """
        audio = self.mode_var.get() == "audio"
        has_ffmpeg = bool(self.ffmpeg_dir)
        self.quality_box.configure(state="disabled" if audio else "readonly")
        self.format_box.configure(state="readonly" if audio else "disabled")
        self.bitrate_box.configure(state="readonly" if audio else "disabled")
        for label, off in ((self.quality_label, audio),
                           (self.format_label, not audio),
                           (self.bitrate_label, not audio)):
            label.configure(style="Muted.TLabel" if off else "TLabel")

        subs_off = audio or not has_ffmpeg
        self.subs_box.configure(state="disabled" if subs_off else "readonly")
        self.subs_label.configure(style="Muted.TLabel" if subs_off else "TLabel")
        self.embed_subs_check.configure(
            state="disabled" if subs_off or self.subs_var.get() == "none" else "normal")
        self.sponsor_check.configure(state="disabled" if not has_ffmpeg else "normal")

        reasons = []
        if audio:
            reasons.append("Resolution and subtitles apply to video downloads.")
        else:
            reasons.append("Audio format and bitrate apply to audio-only downloads.")
        if not has_ffmpeg:
            reasons.append("Subtitles and SponsorBlock need ffmpeg, which is not installed.")
        self.gate_note.configure(text=" ".join(reasons))
        self.describe_preset()

    def sync_login(self, *_args):
        """Say the Chromium thing before it fails, not afterwards."""
        browser = self.browser_var.get()
        if browser != "none" and self.cookie_file_var.get():
            # yt-dlp would be handed both; the file is the one that was chosen
            # most recently by hand, so the browser gives way.
            self.clear_cookie_file()
        if sys.platform == "win32" and browser in ("chrome", "edge", "brave", "chromium",
                                                   "opera", "vivaldi", "whale"):
            self.browser_note.configure(
                text="Close %s completely first, including any icon in the system tray -- "
                     "it keeps its cookies locked while it runs. Firefox does not, and a "
                     "cookies.txt always works." % browser)
        else:
            self.browser_note.configure(text="")
        self.refresh_disclosure()

    def toggle_extras(self):
        self.extras_open = bool(self.disclosure_var.get())
        if self.extras_open:
            self.extras.pack(fill="x", after=self.disclosure.master)
            self.disclosure.configure(text="▾  More options")
        else:
            self.extras.pack_forget()
            self.disclosure.configure(text="▸  More options")
        self.refresh_disclosure()
        if self.extras_open and self.ready:
            # The panel needs room that the queue would otherwise have to give
            # up. Take it from the screen while there is any left.
            self.window.update_idletasks()
            wanted = self.window.winfo_reqheight()
            if self.window.winfo_height() < wanted:
                room = min(wanted, self.window.winfo_screenheight() - 80)
                self.window.geometry("%dx%d" % (self.window.winfo_width(), room))

    def refresh_disclosure(self, *_args):
        """Count what is switched on in there, so folded never means hidden."""
        on = []
        if self.playlist_var.get():
            on.append("playlist")
        if self.subs_var.get() != "none":
            on.append("subtitles")
        if self.sponsor_var.get():
            on.append("SponsorBlock")
        if self.section_start_var.get().strip() or self.section_end_var.get().strip():
            on.append("clip")
        if self.browser_var.get() != "none" or self.cookie_file_var.get():
            on.append("sign-in")
        self.disclosure_note.configure(
            text="" if self.extras_open or not on else "on: " + ", ".join(on))

    def pick_cookie_file(self):
        chosen = filedialog.askopenfilename(
            title="Select a cookies.txt file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        if not chosen:
            return  # Cancel must not wipe what was already chosen.
        self.cookie_file_var.set(chosen)
        self.log("[cookies] file selected: " + os.path.basename(chosen))
        self.browser_var.set("none")
        self.show_cookie_file()

    def clear_cookie_file(self):
        self.cookie_file_var.set("")
        self.show_cookie_file()

    def show_cookie_file(self):
        path = self.cookie_file_var.get()
        self.cookie_name.configure(text=os.path.basename(path) if path else "")
        if path and not self.cookie_clear.winfo_ismapped():
            self.cookie_clear.pack(side="left")
        elif not path and self.cookie_clear.winfo_ismapped():
            self.cookie_clear.pack_forget()

    def choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or self.fallback_dir)
        if chosen:
            self.dest_var.set(chosen)

    def open_folder(self):
        target = self.dest_var.get()
        if not os.path.isdir(target):
            self.say("That folder does not exist yet. It is created when the first "
                     "download starts.", "muted")
            return
        problem = open_in_file_manager(target)
        if problem:
            self.log("[warning] " + problem)

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
                  "detail": None, "installed": None}
        try:
            result["installed"] = installed_ytdlp_version()
            if result["installed"] is None:
                result["problem"] = "not installed"
            else:
                result["latest"] = latest_ytdlp_version()
        except Exception as exc:  # noqa: BLE001
            # Being offline is normal and not worth a dialog. The event has
            # to go out anyway, or the Check button stays disabled for good.
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

        self.engine_ok = result["installed"] is not None
        self.gate_engine()
        if result["installed"] is None:
            self.show_banner("yt-dlp is not installed, so nothing can be downloaded yet.",
                             "Install now")
            self.refresh_status()
            return
        if result["latest"] and parse_version(result["latest"]) > parse_version(result["installed"]):
            self.show_banner(
                "yt-dlp %s is out and you have %s. Downloads that started failing usually "
                "work again after this." % (result["latest"], result["installed"]),
                "Update now")
        elif result["announce"] and result["latest"]:
            self.log("[update] yt-dlp %s is the current release." % result["installed"])
        self.refresh_status()

    def refresh_footer(self, problem=None):
        parts = ["yt-dlp " + self.installed_version if self.installed_version
                 else "yt-dlp missing"]
        parts.append("ffmpeg found" if self.ffmpeg_dir else "no ffmpeg")
        if problem and problem != "not installed":
            parts.append(problem)
        self.footer_var.set("   |   ".join(parts))

    def show_banner(self, text, button_label):
        self.banner_var.set(text)
        if button_label:
            self.update_btn.configure(text=button_label, state="normal")
            if not self.update_btn.winfo_ismapped():
                self.update_btn.pack(side="right", padx=self.theme.pad("sm", "sm"))
        elif self.update_btn.winfo_ismapped():
            self.update_btn.pack_forget()
        self.dismiss_btn.configure(text="Later")
        self.banner.pack(fill="x", side="top", before=self.top)

    def hide_banner(self):
        self.banner.pack_forget()

    def start_update(self):
        self.update_btn.configure(state="disabled")
        self.banner_var.set("Updating yt-dlp...")
        self.log("[update] python -m pip install -U yt-dlp")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            ok, message = update_ytdlp(lambda line: self.events.put(("log", line)))
        except Exception as exc:  # noqa: BLE001
            # Or the banner sits on "Updating yt-dlp..." with a dead button.
            ok, message = False, "The update could not run: %r" % (exc,)
        self.events.put(("updated", (ok, message)))

    def on_updated(self, ok, message):
        self.log("[update] " + message)
        self.banner_var.set(message)
        if ok:
            self.update_btn.pack_forget()
            self.dismiss_btn.configure(text="OK")
            self.installed_version = installed_ytdlp_version()
            self.latest_version = self.installed_version
            self.engine_ok = self.installed_version is not None
            self.gate_engine()
            self.refresh_footer()
            self.hint = self.stale_hint()
            self.refresh_status()
        else:
            self.update_btn.configure(state="normal")

    def gate_engine(self):
        """A button that cannot do its job should not be pressable.

        The banner used to say nothing could be downloaded while Download sat
        there fully enabled, which is an invitation to press it and get a
        pip command in a dialog box.
        """
        state = "disabled" if self.engine_ok is False else "normal"
        self.download_btn.configure(state=state)
        self.preview_btn.configure(state=state)
        if self.engine_ok is False:
            self.say("yt-dlp is missing, so there is nothing to download with. "
                     "Use 'Install now' at the top of the window.")

    def stale_hint(self):
        """What to tell a worker to say when an extractor looks out of date."""
        if (self.latest_version and self.installed_version
                and parse_version(self.latest_version) > parse_version(self.installed_version)):
            return ("yt-dlp %s is out and you are on %s. Click '%s' at the top of the window, "
                    "then use Retry on this row."
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
                elif kind == "item-progress":
                    # An event still queued when the item ended must not write
                    # over how it ended.
                    item, pct, stats = payload
                    if item.finished:
                        continue
                    item.last_event = time.monotonic()
                    item.progress = pct
                    item.detail = format_progress(stats, verbose=False)
                    if item.status in ("Starting", "Retrying"):
                        item.status = "Downloading"
                        self.refresh_status()
                    if stats:
                        self.active_var.set(format_progress(stats, verbose=True))
                    self.refresh_row(item)
                    self.refresh_bar()
                elif kind == "item-title":
                    item, title = payload
                    if title and title != item.label:
                        item.label = title
                        self.refresh_row(item)
                elif kind == "item-stage":
                    item, stage, before_download = payload
                    if item.finished:
                        continue
                    item.last_event = time.monotonic()
                    # A subtitle convertor runs before the download, not after
                    # it. Calling that "Processing" left every row claiming to
                    # be finished downloading for the whole download.
                    if not before_download and item.status in ("Starting", "Downloading"):
                        item.status = "Converting"
                        self.refresh_status()
                    item.detail = stage_label(stage)
                    self.refresh_row(item)
                    self.refresh_bar()
                elif kind == "preview":
                    self.on_preview(*payload)
                elif kind == "queue-idle":
                    self.on_queue_idle()
        except queue.Empty:
            pass
        self.watch_for_stalls()
        self.window.after(120, self.pump)

    def watch_for_stalls(self):
        """yt-dlp retries quietly, five times, with warnings switched off.

        From outside, a row that is retrying and a row that has hung look
        exactly alike: both stop moving and say nothing.
        """
        now = time.monotonic()
        changed = False
        for item in list(self.rows.values()):
            if item.status == "Downloading" and now - item.last_event > STALL_SECONDS:
                item.status = "Retrying"
                item.detail = "no data for %ds - retrying" % int(now - item.last_event)
                self.refresh_row(item)
                changed = True
        if changed:
            self.refresh_status()
            self.refresh_bar()

    def on_queue_idle(self):
        self.refresh_status()
        self.active_var.set("")
        failed = [i for i in self.rows.values() if i.status in ("Failed", "Partial")]
        done = [i for i in self.rows.values() if i.status == "Done"]
        if done or failed:
            # Nothing reached an unfocused window before this: no sound, no
            # taskbar change, nothing. For an app whose whole premise is
            # "start it and go and do something else" that was the biggest
            # gap in it.
            try:
                self.window.bell()
            except Exception:  # noqa: BLE001
                pass
        self.window.title(self.base_title)

    # ------------------------------------------------------------------ queue
    def pending_urls(self):
        return [line.strip() for line in self.urls_box.get("1.0", "end").splitlines()
                if line.strip()]

    def clip_range(self):
        """The two time fields as seconds, or None when they make no sense."""
        for widget, variable, name in ((self.section_start, self.section_start_var, "from"),
                                       (self.section_end, self.section_end_var, "to")):
            try:
                parse_timestamp(variable.get())
            except ValueError as exc:
                self.open_extras()
                widget.focus_set()
                self.say("'%s' in the '%s' box is not a time. Write 1:30, or 90, "
                         "or 1:02:03." % (exc, name))
                return None
        start = parse_timestamp(self.section_start_var.get())
        end = parse_timestamp(self.section_end_var.get())
        if start is not None and end is not None and end <= start:
            self.open_extras()
            self.section_end.focus_set()
            self.say("The end of the clip has to come after its start.")
            return None
        return (start, end)

    def open_extras(self):
        if not self.extras_open:
            self.disclosure_var.set(True)
            self.toggle_extras()

    def download_settings(self):
        """Everything a queue item needs, as it stands right now, or None if
        the form does not add up."""
        clip = self.clip_range()
        if clip is None:
            return None
        out_dir = self.dest_var.get().strip() or self.fallback_dir
        self.dest_var.set(out_dir)
        return dict(self.current_settings(),
                    out_dir=out_dir,
                    playlist=self.playlist_var.get(),
                    subtitles=self.subs_var.get(),
                    embed_subs=self.embed_subs_var.get(),
                    sponsorblock=self.sponsor_var.get(),
                    section_start=clip[0],
                    section_end=clip[1],
                    browser=self.browser_var.get(),
                    cookie_file=self.cookie_file_var.get() or None)

    def start(self):
        """Put the links in the box on the queue and make sure it is moving."""
        self.hush()
        urls = self.pending_urls()
        if not urls:
            self.say("Paste at least one link first.")
            self.urls_box.focus_set()
            return
        good = [url for url in urls if url.lower().startswith(("http://", "https://"))]
        bad = [url for url in urls if url not in good]
        if not good:
            self.say("That does not look like a link. A link starts with http:// "
                     "or https://.")
            self.urls_box.focus_set()
            return
        settings = self.download_settings()
        if settings is None:
            return
        self.enqueue(good, settings)
        self.urls_box.delete("1.0", "end")
        if bad:
            # Keep what could not be used rather than swallowing it.
            self.urls_box.insert("1.0", "\n".join(bad))
            self.say("%d line%s did not look like a link and %s left in the box."
                     % (len(bad), "" if len(bad) == 1 else "s",
                        "was" if len(bad) == 1 else "were"), "warning")

    def enqueue(self, urls, settings, label=None):
        self.hide_empty_row()
        for url in urls:
            self.next_uid += 1
            item = QueueItem(self.next_uid, url, dict(settings), label=label)
            with self.lock:
                self.items.append(item)
            self.rows[item.uid] = item
            self.tree.insert("", "end", iid=str(item.uid), text=item.label,
                             values=(item.summary, "Queued", "waiting"),
                             tags=("Queued",))
        last = str(self.next_uid)
        self.tree.see(last)
        # Without a focus item the Treeview ignores the arrow keys entirely,
        # which took pause, resume, cancel and remove with it.
        if not self.tree.focus():
            self.tree.focus(last)
            self.tree.selection_set(last)
        self.refresh_status()
        self.ensure_worker()

    def show_empty_row(self):
        if not self.rows and not self.tree.exists(EMPTY_ROW):
            self.tree.insert("", "end", iid=EMPTY_ROW,
                             text="Nothing queued yet.",
                             values=("", "", "Paste a link above and press Download."),
                             tags=(EMPTY_ROW,))

    def hide_empty_row(self):
        if self.tree.exists(EMPTY_ROW):
            self.tree.delete(EMPTY_ROW)

    # ---------------------------------------------------------------- preview
    def preview(self):
        """Ask the site what the first link holds, before committing to it."""
        self.hush()
        urls = self.pending_urls()
        if not urls:
            self.say("Paste a link first.")
            self.urls_box.focus_set()
            return
        settings = self.download_settings()
        if settings is None:
            return
        if len(urls) > 1:
            self.say("Preview reads the first link; the rest stay in the box.", "muted")
        self.preview_btn.configure(state="disabled")
        self.preview_var.set("Reading %s ..." % urls[0])
        if not self.preview_line.winfo_ismapped():
            self.preview_line.pack(anchor="w")
        threading.Thread(target=self._preview_worker, args=(urls[0], settings),
                         daemon=True).start()

    def _preview_worker(self, url, settings):
        converter = Converter(on_log=lambda msg: self.events.put(("log", str(msg))),
                              browser=settings.get("browser"),
                              cookie_file=settings.get("cookie_file"),
                              update_hint=self.hint)
        try:
            data = converter.inspect(url, settings)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("preview", (None, converter._explain(exc), settings)))
            return
        data["thumbnail_file"] = fetch_thumbnail(data.get("thumbnail"), self.ffmpeg_dir)
        self.events.put(("preview", (data, None, settings)))

    def on_preview(self, data, problem, settings):
        self.preview_btn.configure(state="normal")
        self.preview_var.set("")
        self.preview_line.pack_forget()
        if problem:
            self.log("[error] " + problem)
            self.say(problem.splitlines()[0])
            if len(problem.splitlines()) > 1:
                self.log(problem)
            return
        self.show_preview(data, settings)

    def show_preview(self, data, settings):
        window = tk.Toplevel(self.window)
        window.title("Preview - " + data["title"][:60])
        window.transient(self.window)
        window.bind("<Escape>", lambda _event: window.destroy())

        buttons = ttk.Frame(window, padding=(self.theme.px("md"), 0,
                                             self.theme.px("md"), self.theme.px("md")))
        # Packed to the bottom before the body claims the space, or the two
        # actions are the first things a small window clips.
        buttons.pack(side="bottom", fill="x")

        head = ttk.Frame(window, padding=self.theme.px("md"))
        head.pack(fill="x")

        picture = data.get("thumbnail_file")
        if picture:
            try:
                window._thumb = tk.PhotoImage(file=picture)
                ttk.Label(head, image=window._thumb).pack(side="left",
                                                          padx=(0, self.theme.px("md")),
                                                          anchor="n")
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    os.remove(picture)  # Tk has read it into memory by now
                except OSError:
                    pass

        text = ttk.Frame(head)
        text.pack(side="left", fill="x", expand=True)
        wrap = 700 if not picture else 420
        ttk.Label(text, text=data["title"], style="Head.TLabel",
                  wraplength=wrap, justify="left").pack(anchor="w")

        facts = [part for part in (data["uploader"], format_duration(data["duration"]),
                                   format_date(data["upload_date"])) if part]
        if data["playlist"]:
            facts.append("in a playlist of %s" % data["playlist"]["count"])
        ttk.Label(text, text="   ".join(facts), style="Muted.TLabel").pack(anchor="w")

        chosen = data["chosen"]
        ttk.Label(text, text=describe_settings(settings, bool(self.ffmpeg_dir)),
                  wraplength=wrap, justify="left").pack(anchor="w",
                                                        pady=(self.theme.px("md"), 0))
        clipped = "" if not chosen["clip"] else ", clipped to %s" % chosen["clip"]
        ttk.Label(text, text="anydl would fetch %s (%s), about %s%s" % (
            chosen["format_id"], chosen["ext"], format_size(chosen["bytes"]), clipped),
            wraplength=wrap, justify="left").pack(anchor="w")
        for part in chosen["parts"]:
            ttk.Label(text, text="      %s  %s  %s  %s" % (
                part["id"], part["resolution"], part["vcodec"] or part["acodec"],
                format_size(part["bytes"])), style="Muted.TLabel").pack(anchor="w")
        if data["converted"] and data["converted"]["bytes"]:
            ttk.Label(text, text="      the converted file lands around %s"
                      % format_size(data["converted"]["bytes"]),
                      style="Muted.TLabel").pack(anchor="w")

        body = ttk.Frame(window, padding=(self.theme.px("md"), self.theme.px("sm"),
                                          self.theme.px("md"), self.theme.px("md")))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Everything the site offers:",
                  style="Section.TLabel").pack(anchor="w")
        scrollbar = ttk.Scrollbar(body)
        scrollbar.pack(side="right", fill="y")
        columns = ("ext", "resolution", "fps", "video", "audio", "size", "note")
        table = ttk.Treeview(body, columns=columns, show="tree headings",
                             yscrollcommand=scrollbar.set, style="Queue.Treeview")
        table.heading("#0", text="id")
        table.column("#0", width=90, stretch=False)
        for name, width in zip(columns, (50, 100, 45, 80, 80, 80, 150)):
            table.heading(name, text=name)
            table.column(name, width=width, stretch=(name == "note"))
        for fmt in data["formats"]:
            table.insert("", "end", text=fmt["id"], values=(
                fmt["ext"], fmt["resolution"], fmt["fps"], fmt["vcodec"], fmt["acodec"],
                format_size(fmt["bytes"]), fmt["note"]))
        table.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=table.yview)

        def add_and_close():
            # The title is already known here; queueing by URL alone left the
            # row showing a raw link until it started downloading.
            self.enqueue([data["url"]], settings, label=data["title"])
            self.drop_from_box(data["url"])
            window.destroy()

        add = ttk.Button(buttons, text="Add to queue", style="Primary.TButton",
                         command=add_and_close)
        add.pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")

        window.update_idletasks()
        window.minsize(640, 420)
        window.geometry("%dx%d" % (max(760, window.winfo_reqwidth()),
                                   min(self.window.winfo_screenheight() - 120,
                                       max(520, window.winfo_reqheight()))))
        add.focus_set()

    def drop_from_box(self, url):
        lines = [line for line in self.pending_urls() if line != url]
        self.urls_box.delete("1.0", "end")
        self.urls_box.insert("1.0", "\n".join(lines))

    # -------------------------------------------------------------- clipboard
    def on_watch_toggled(self):
        """Remember what is on the clipboard now, so turning the watch on does
        not sweep up something copied an hour ago."""
        self.last_clipboard = self.read_clipboard()
        self.log("[clipboard] watching for copied links."
                 if self.watch_clipboard_var.get() else "[clipboard] no longer watching.")

    def read_clipboard(self):
        try:
            return (self.window.clipboard_get() or "").strip()
        except Exception:  # noqa: BLE001
            # Empty, holding an image, or momentarily owned by another app.
            return None

    def poll_clipboard(self):
        if self.watch_clipboard_var.get():
            text = self.read_clipboard()
            # A failed read is not an empty clipboard. Treating it as one reset
            # the memory and let the same link come back in a second time.
            if text is not None and text != self.last_clipboard:
                self.last_clipboard = text
                self.take_copied_link(text)
        self.window.after(700, self.poll_clipboard)

    def take_copied_link(self, text):
        if len(text.split()) != 1 or not text.lower().startswith(("http://", "https://")):
            return
        if text in self.pending_urls():
            return
        # Appended rather than rewritten: rebuilding the whole box threw away
        # the caret and any half-typed line.
        current = self.urls_box.get("1.0", "end-1c")
        if current and not current.endswith("\n"):
            self.urls_box.insert("end", "\n")
        self.urls_box.insert("end", text + "\n")
        self.urls_box.see("end")
        self.log("[clipboard] added " + text)
        self.say("Added a link from the clipboard.", "muted")

    def ensure_worker(self):
        with self.lock:
            if self.worker_running:
                return
            self.worker_running = True
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _next_item(self):
        """Claim the next waiting item, or release the worker if there is none.

        Both happen under the lock start() takes, so a link added while the
        worker is winding up cannot end up sitting there with nothing running.
        """
        with self.lock:
            for item in self.items:
                if item.status == "Queued" and not item.cancelled:
                    item.status = "Starting"
                    item.last_event = time.monotonic()
                    return item
            self.worker_running = False
            return None

    def _queue_worker(self):
        try:
            while True:
                item = self._next_item()
                if item is None:
                    self.events.put(("queue-idle", None))
                    return
                try:
                    self._run_item(item)
                except Exception as exc:  # noqa: BLE001
                    # Whatever went wrong belongs to that one link.
                    self._fail_item(item, "%s: %s" % (type(exc).__name__, exc))
        except BaseException as exc:  # noqa: BLE001
            # A worker that dies without clearing this leaves the queue looking
            # busy for ever, and every link added afterwards just sits there.
            with self.lock:
                self.worker_running = False
            self.events.put(("log", "[error] the queue stopped: %r" % (exc,)))
            self.events.put(("queue-idle", None))
            raise

    def _fail_item(self, item, message):
        with self.lock:
            item.status, item.detail, item.converter = "Failed", message, None
            item.note = message
        self.events.put(("log", "[error] " + message))
        self.events.put(("item", item))

    def _run_item(self, item):
        converter = Converter(
            on_log=lambda msg: self.events.put(("log", str(msg))),
            on_progress=lambda pct, stats, this=item: self.events.put(
                ("item-progress", (this, pct, stats))),
            on_title=lambda title, this=item: self.events.put(("item-title", (this, title))),
            on_stage=lambda stage, before, this=item: self.events.put(
                ("item-stage", (this, stage, before))),
            browser=item.settings.get("browser"),
            cookie_file=item.settings.get("cookie_file"),
            update_hint=self.hint,
        )
        with self.lock:
            # A cancel that arrived while this was being set up would have
            # had nothing to set the flag on.
            converter.cancelled = item.cancelled or item.pause_requested
            converter.set_rate_limit(self.rate_limit)
            item.converter = converter

        self.events.put(("item", item))
        self.events.put(("log", "\n>> " + item.url))
        converter.announce_cookies()
        outcome, message = converter.run(item.url, item.settings)

        with self.lock:
            item.converter = None
            item.note = message
            if item.cancelled:
                item.status = "Cancelled"
                item.detail = ("Cancelled at %.0f%% - the partial file is kept, so queueing "
                               "this link again carries on from here" % item.progress
                               if item.progress > 0 else "Cancelled before it started")
            elif item.pause_requested:
                item.pause_requested = False
                item.status = "Paused"
                item.detail = "Paused at %.0f%% - Resume carries on from here" % item.progress
            elif outcome == "ok":
                item.status, item.progress, item.detail = "Done", 100.0, message
            elif outcome == "partial":
                item.status, item.progress, item.detail = "Partial", 100.0, message
            else:
                item.status, item.detail = "Failed", message
        if item.status == "Failed":
            self.events.put(("log", "[error] " + message))
        elif item.status == "Partial":
            self.events.put(("log", "[warning] " + message))
        elif item.status == "Done":
            self.events.put(("log", "[done] %s -> %s" % (item.label,
                                                         item.settings["out_dir"])))
        elif item.status == "Cancelled" and item.progress > 0:
            # yt-dlp keeps what it had as a .part file, on purpose: queueing
            # the same link again picks up where this left off.
            self.events.put(("log", "[cancelled] %s - the partial file is still in the "
                                    "folder, and downloading it again resumes from there."
                             % item.label))
        self.events.put(("item", item))

    def on_rate_changed(self, *_args):
        """The cap applies to what is downloading now, not only to the next one."""
        megabytes = round(self.rate_var.get() * 2) / 2.0
        self.rate_limit = int(megabytes * 1048576) or None
        self.rate_text.set("%.1f MB/s" % megabytes if self.rate_limit else "no limit")
        with self.lock:
            live = [item.converter for item in self.items if item.converter]
        for converter in live:
            converter.set_rate_limit(self.rate_limit)

    # ------------------------------------------------------------ pause
    def pause_item(self, item):
        """Stop the download but keep the item, and the bytes already on disk.

        yt-dlp leaves a .part file behind and picks it up when the same link
        runs again, so resuming is a matter of putting the item back in line.
        """
        with self.lock:
            if item.status not in ("Downloading", "Retrying", "Starting"):
                return
            item.pause_requested = True
            item.status = "Pausing"
            # Or the row keeps the speed and the ETA it had a moment ago and
            # looks like it is still going at full tilt.
            item.detail = "Stopping - the bytes already on disk are kept"
            if item.converter:
                item.converter.cancelled = True
        self.refresh_row(item)
        self.refresh_status()

    def resume_item(self, item):
        with self.lock:
            if item.status != "Paused":
                return
            item.pause_requested = False
            item.status, item.detail = "Queued", "waiting"
        self.refresh_row(item)
        self.refresh_status()
        self.ensure_worker()

    def on_pause_pressed(self):
        running = [item for item in self.rows.values()
                   if item.status in ("Downloading", "Retrying", "Starting")]
        if running:
            for item in running:
                self.pause_item(item)
            return
        for item in list(self.rows.values()):
            if item.status == "Paused":
                self.resume_item(item)

    def refresh_pause_button(self):
        """Four labels, not two.

        Pressing Pause moves the row to Pausing, which used to match neither
        branch: the button greyed out still saying Pause, then silently turned
        into Resume whenever the worker got round to unwinding.
        """
        statuses = [item.status for item in self.rows.values()]
        moving = [s for s in statuses if s in ("Downloading", "Retrying", "Starting")]
        if "Pausing" in statuses:
            self.pause_btn.configure(text="Pausing...", state="disabled")
        elif moving:
            self.pause_btn.configure(
                text="Pause all" if len(moving) > 1 else "Pause", state="normal")
        elif "Paused" in statuses:
            paused = statuses.count("Paused")
            self.pause_btn.configure(
                text="Resume all" if paused > 1 else "Resume", state="normal")
        else:
            self.pause_btn.configure(text="Pause", state="disabled")

    def refresh_queue_tools(self):
        """A button that would do nothing should say so before it is pressed."""
        chosen = self.selected_items()
        alive = [item for item in self.rows.values() if not item.finished]
        self.cancel_btn.configure(
            state="normal" if any(not i.finished for i in chosen) else "disabled")
        self.cancel_all_btn.configure(state="normal" if alive else "disabled")
        self.clear_btn.configure(
            state="normal" if any(i.finished for i in self.rows.values()) else "disabled")

    def selected_items(self):
        chosen = []
        for uid in self.tree.selection():
            if uid.isdigit() and int(uid) in self.rows:
                chosen.append(self.rows[int(uid)])
        return chosen

    def sync_row_menu(self):
        """Grey what does not apply, rather than returning silently."""
        chosen = self.selected_items()
        def state(condition):
            return "normal" if condition else "disabled"
        self.row_menu.entryconfigure(
            "Retry", state=state(any(i.status in ("Failed", "Partial", "Cancelled")
                                     for i in chosen)))
        self.row_menu.entryconfigure(
            "Pause", state=state(any(i.status in ("Downloading", "Retrying", "Starting")
                                     for i in chosen)))
        self.row_menu.entryconfigure(
            "Resume", state=state(any(i.status == "Paused" for i in chosen)))
        self.row_menu.entryconfigure("Copy link", state=state(len(chosen) == 1))
        self.row_menu.entryconfigure(
            "Why did it fail?", state=state(any(i.note for i in chosen)))
        self.row_menu.entryconfigure("Cancel", state=state(any(not i.finished for i in chosen)))
        self.row_menu.entryconfigure("Remove", state=state(any(not i.busy for i in chosen)))
        self.row_menu.entryconfigure("Open folder", state=state(len(chosen) == 1))

    def show_row_menu(self, event=None):
        if event is not None and getattr(event, "y", None) is not None and event.num in (2, 3):
            row = self.tree.identify_row(event.y)
            if row and row not in self.tree.selection():
                self.tree.selection_set(row)
            where = (event.x_root, event.y_root)
        else:
            # Reached from the keyboard, so there is no pointer to ask.
            chosen = self.tree.selection()
            if not chosen:
                return "break"
            box = self.tree.bbox(chosen[0])
            if not box:
                return "break"
            where = (self.tree.winfo_rootx() + box[0] + box[2] // 2,
                     self.tree.winfo_rooty() + box[1] + box[3])
        if not self.selected_items():
            return "break"
        try:
            self.row_menu.tk_popup(where[0], where[1])
        finally:
            self.row_menu.grab_release()
        return "break"

    def copy_selected_link(self):
        chosen = self.selected_items()
        if not chosen:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(chosen[0].url)
        self.say("Link copied.", "muted")

    def show_row_detail(self):
        """The whole reason, not the first line of it.

        refresh_row keeps one line, and the column clips that -- so the half of
        the message that says what to do about it never reached anybody.
        """
        chosen = [item for item in self.selected_items() if item.note]
        if not chosen:
            return
        item = chosen[0]
        window = tk.Toplevel(self.window)
        window.title("%s - %s" % (STATUS_TABLE[item.status]["label"], APP_NAME))
        window.transient(self.window)
        window.bind("<Escape>", lambda _event: window.destroy())
        frame = ttk.Frame(window, padding=self.theme.px("md"))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=item.label, style="Section.TLabel",
                  wraplength=520, justify="left").pack(anchor="w")
        ttk.Label(frame, text=item.url, style="Muted.TLabel",
                  wraplength=520, justify="left").pack(anchor="w",
                                                       pady=(0, self.theme.px("sm")))
        ttk.Label(frame, text=item.note, wraplength=520,
                  justify="left").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(self.theme.px("md"), 0))
        if item.status in ("Failed", "Partial", "Cancelled"):
            ttk.Button(row, text="Retry", style="Primary.TButton",
                       command=lambda: [self.retry_item(item), window.destroy()]).pack(side="left")
        ttk.Button(row, text="Copy link",
                   command=lambda: [self.window.clipboard_clear(),
                                    self.window.clipboard_append(item.url)]).pack(
            side="left", padx=(self.theme.px("sm"), 0))
        close = ttk.Button(row, text="Close", command=window.destroy)
        close.pack(side="right")
        close.focus_set()

    def retry_item(self, item):
        """Put the same link back with the same settings.

        The app already told people to update yt-dlp and try the link again --
        and then gave them no way to, because start() had cleared the box and
        the URL only survived inside a Treeview cell.
        """
        self.enqueue([item.url], item.settings, label=item.label)

    def retry_selected(self):
        for item in self.selected_items():
            if item.status in ("Failed", "Partial", "Cancelled"):
                self.retry_item(item)

    def cancel_item(self, item):
        if item.finished:
            return
        with self.lock:
            item.cancelled = True
            if item.status == "Queued":
                item.status, item.detail = "Cancelled", "Cancelled before it started"
            else:
                item.status = "Cancelling"
                item.detail = ("Stopping - the bytes already on disk are kept"
                               if item.progress > 0 else "Stopping...")
                if item.converter:
                    item.converter.cancelled = True
        self.refresh_row(item)
        self.refresh_status()

    def cancel_selected(self):
        for item in self.selected_items():
            self.cancel_item(item)

    def cancel_all(self):
        live = [item for item in self.rows.values() if item.busy]
        if live and not messagebox.askyesno(
                APP_NAME,
                "Stop %d download%s in progress?\n\nWhat has already been fetched stays "
                "on disk, so queueing the same link again carries on from there."
                % (len(live), "" if len(live) == 1 else "s")):
            return
        for item in list(self.rows.values()):
            self.cancel_item(item)

    def clear_finished(self):
        self._remove([item for item in self.rows.values() if item.finished])

    def clear_selected(self):
        # Anything that is not moving can go, including a paused or waiting one.
        chosen = self.selected_items()
        doomed = [item for item in chosen if not item.busy]
        if chosen and not doomed:
            self.say("That row is still running. Cancel it first.", "muted")
            return
        self._remove(doomed)

    def _remove(self, doomed):
        if not doomed:
            return
        with self.lock:
            for item in doomed:
                self.items.remove(item)
        following = None
        for item in doomed:
            if self.tree.exists(str(item.uid)):
                following = self.tree.next(str(item.uid)) or self.tree.prev(str(item.uid))
            self.rows.pop(item.uid, None)
            self.tree.delete(str(item.uid))
        # Keep a focus item, or the arrow keys go dead again.
        if following and self.tree.exists(following):
            self.tree.focus(following)
            self.tree.selection_set(following)
        self.show_empty_row()
        self.refresh_status()

    def open_item_folder(self, _event=None):
        for item in self.selected_items():
            target = item.settings["out_dir"]
            if not os.path.isdir(target):
                self.say("%s does not exist yet." % target, "muted")
                return "break"
            problem = open_in_file_manager(target)
            if problem:
                self.log("[warning] " + problem)
            return "break"
        return "break"

    def on_close(self):
        live = [item for item in self.rows.values() if item.busy]
        if live and not messagebox.askyesno(
                APP_NAME,
                "%d download%s still running.\n\nClosing now stops %s. What has already "
                "been fetched stays on disk, so starting the same link again carries on "
                "from there.\n\nClose anyway?"
                % (len(live), " is" if len(live) == 1 else "s are",
                   "it" if len(live) == 1 else "them")):
            return
        self.window.destroy()

    # ------------------------------------------------------------- queue view
    def refresh_row(self, item):
        row = str(item.uid)
        if not self.tree.exists(row):
            return
        detail = item.detail.splitlines()[0] if item.detail else ""
        if item.status in ("Downloading", "Retrying"):
            detail = "%s %5.1f%%  %s" % (progress_bar(item.progress), item.progress, detail)
        elif item.status in ("Converting", "Pausing", "Cancelling"):
            # No bar at all here: a full one beside "Processing" read as
            # finished, and that is the longest wait in the whole app.
            detail = detail or STATUS_TABLE[item.status]["label"]
        elif item.status == "Paused":
            detail = "%s  %s" % (progress_bar(item.progress), detail)
        elif item.status == "Done":
            detail = "%s  %s" % (progress_bar(100), detail or "done")
        self.tree.item(row, text=item.label,
                       values=(item.summary, STATUS_TABLE[item.status]["label"], detail),
                       tags=(item.status,))

    def refresh_status(self):
        self.refresh_pause_button()
        self.refresh_queue_tools()
        self.refresh_bar()
        counts = {}
        for item in self.rows.values():
            counts[item.status] = counts.get(item.status, 0) + 1
        if not counts:
            self.status_var.set("Ready.")
            self.active_var.set("")
            self.window.title(self.base_title)
            return
        self.status_var.set("   ".join(
            "%d %s" % (counts[name], STATUS_TABLE[name]["label"].lower())
            for name in STATUS_ORDER if counts.get(name)))
        # The title bar is the one completion channel that reaches an
        # unfocused window for free, and it shows up in Alt-Tab too.
        busy = [item for item in self.rows.values() if item.busy]
        if busy:
            done = sum(1 for item in self.rows.values() if item.finished)
            self.window.title("%d%% - %d of %d - %s" % (
                self._queue_fraction() * 100, done + 1, len(self.rows), self.base_title))
        else:
            self.window.title(self.base_title)

    def _queue_fraction(self):
        """How far the whole queue has got, not how far one item has."""
        if not self.rows:
            return 0.0
        total = len(self.rows)
        done = sum(1 for item in self.rows.values() if item.finished)
        running = sum(item.progress / 100.0 for item in self.rows.values()
                      if item.busy and item.status in ("Downloading", "Retrying"))
        return min(1.0, (done + running) / total)

    def refresh_bar(self):
        """One bar for the queue, and honest about not knowing.

        It used to follow whichever item was running, so it swept 0 to 100 once
        per link; it stayed at 100 after one finished; it sat at 100 for the
        whole of a conversion; and it showed a failure as 0, which is
        indistinguishable from not having started.
        """
        vague = [item for item in self.rows.values()
                 if item.status in ("Starting", "Converting", "Retrying", "Pausing",
                                    "Cancelling")
                 or (item.status == "Downloading" and item.progress <= 0)]
        want = "indeterminate" if vague else "determinate"
        if want != self.bar_mode:
            self.bar_mode = want
            if want == "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(60)
            else:
                self.bar.stop()
                self.bar.configure(mode="determinate")
        if want == "determinate":
            self.bar["value"] = self._queue_fraction() * 100


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
    parser.add_argument("--from", dest="section_start", metavar="TIME", default=None,
                        help="start the file at this point: 1:30, 90 or 1:02:03")
    parser.add_argument("--to", dest="section_end", metavar="TIME", default=None,
                        help="and stop it here (needs ffmpeg)")
    parser.add_argument("--limit-rate", dest="limit_rate", metavar="MB", type=float,
                        default=None, help="cap the download at this many MB per second")
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
        print(describe_settings(settings, bool(find_ffmpeg())))

    try:
        section_start = parse_timestamp(args.section_start)
        section_end = parse_timestamp(args.section_end)
    except ValueError as exc:
        parser.error("'%s' is not a time. Write 1:30, or 90, or 1:02:03." % exc)

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
    if args.limit_rate:
        converter.set_rate_limit(int(args.limit_rate * 1048576))
    converter.download(args.url, args.output, settings["mode"], settings["quality"],
                       settings["audio_format"], settings["bitrate"], args.playlist,
                       extras={"subtitles": args.subs,
                               "embed_subs": args.embed_subs,
                               "sponsorblock": args.sponsorblock,
                               "section_start": section_start,
                               "section_end": section_end})


if __name__ == "__main__":
    main()
