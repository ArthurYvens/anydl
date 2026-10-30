"""
anydl - download video as MP4 from ~1700 sites, or extract just the audio
(MP3, M4A, WAV, OPUS, FLAC). Anything yt-dlp supports: YouTube, Instagram,
X/Twitter, TikTok, Vimeo, SoundCloud, Twitch and the rest.

    python anydl.py                 open the graphical interface
    python anydl.py URL --audio     command line mode

Requires yt-dlp. ffmpeg is optional but strongly recommended: without it the
app falls back to single-stream video (~720p) and cannot transcode audio.

The code lives in the anydl_core package beside this file. This module is the
entry point and the public surface: it re-exports the names other things -- the
test suite, a script someone wrote against it -- expect to find on `anydl`, so
that where a class lives can change without breaking them.
"""

import os
import sys

# The package sits next to this file, and this file gets loaded by absolute
# path (the test suite does exactly that), in a process where the project root
# is not on sys.path and the imports below would fail.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from anydl_core.captions import fetch_transcript, tidy_transcript  # noqa: E402
from anydl_core.cli import main, report_versions  # noqa: E402
from anydl_core.config import (  # noqa: E402
    ANSI,
    APP_DIR,
    APP_NAME,
    AUDIO_BITRATES,
    AUDIO_FORMATS,
    BIN_DIR,
    BROWSERS,
    CUSTOM_PRESET,
    DARK_PALETTE,
    EMPTY_ROW,
    IDLE_STATUS,
    LIGHT_PALETTE,
    PRESETS,
    SPACE,
    SPONSOR_CATEGORIES,
    STAGE_LABELS,
    STALE_MARKERS,
    STALL_SECONDS,
    STATUS_ORDER,
    STATUS_TABLE,
    SUBTITLE_LANGS,
    UPDATE_HINT,
    VIDEO_QUALITIES,
    YTDLP_PYPI_URL,
)
from anydl_core.converter import Converter, _LoggerAdapter  # noqa: E402
from anydl_core.environment import (  # noqa: E402
    apply_window_icon,
    claim_taskbar_identity,
    default_output_dir,
    fetch_thumbnail,
    ffmpeg_tool,
    find_ffmpeg,
    installed_ytdlp_version,
    latest_ytdlp_version,
    open_in_file_manager,
    probe_height,
    python_executable,
    update_ytdlp,
)
from anydl_core.library import LibraryIndex  # noqa: E402
from anydl_core.media import ClipSource  # noqa: E402
from anydl_core.text import (  # noqa: E402
    audio_size_estimate,
    clock_label,
    describe_existing,
    describe_settings,
    find_preset,
    format_date,
    format_duration,
    format_progress,
    format_size,
    normalise_stage,
    parse_timestamp,
    parse_version,
    progress_bar,
    section_suffix,
    stage_label,
    stamp_label,
    wanted_height,
)

# Everything above imports without a display, and without tkinter installed at
# all. The names below do not, so they are fetched on first use: the command
# line has to keep working on a machine that has no tkinter.
_WINDOW_NAMES = {
    "Theme": "theme",
    "ClipEditor": "trim_window",
    "QueueItem": "main_window",
    "AnydlApp": "main_window",
    "launch_gui": "main_window",
}


def __getattr__(name):
    """Resolve a window class the first time something asks for it."""
    module = _WINDOW_NAMES.get(name)
    if module is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    import importlib

    return getattr(importlib.import_module("anydl_core." + module), name)


def __dir__():
    return sorted(list(globals()) + list(_WINDOW_NAMES))


def _load_tk():
    """Publish the tkinter names, for callers that prepared Tk by hand.

    This was how the single-file version deferred `import tkinter` so the
    command line would run on a machine without it. The window modules import
    tkinter themselves now, and are themselves imported lazily, which does the
    same job without module-level globals. What is left is the handful of
    names that used to appear here afterwards -- `anydl.messagebox` in
    particular, which tests replace to answer a dialog.
    """
    import tkinter
    from tkinter import filedialog, font, messagebox, ttk

    module = sys.modules[__name__]
    for name, value in (("tk", tkinter), ("ttk", ttk), ("filedialog", filedialog),
                        ("messagebox", messagebox), ("tkfont", font)):
        setattr(module, name, value)
    return tkinter


if __name__ == "__main__":
    main()
