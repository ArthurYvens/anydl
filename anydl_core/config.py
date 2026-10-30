"""Constants: names, palettes, spacing, presets and the status table."""

import os
import re

# The project root, one level up: bin/ and assets/ sit beside anydl.py,
# not inside this package.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BIN_DIR = os.path.join(APP_DIR, "bin")


APP_NAME = "anydl"


VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]


AUDIO_FORMATS = ["mp3", "m4a", "wav", "opus", "flac"]


AUDIO_BITRATES = ["320", "256", "192", "160", "128", "96"]


# The name carries the intent; the line under the buttons carries the numbers,
# derived from the settings themselves so the two cannot drift. "cli" is the
# stable identifier and does not change with the label.
PRESETS = [
    {"name": "Music", "cli": "music",
     "settings": {"mode": "audio", "audio_format": "mp3", "bitrate": "320"}},
    {"name": "High quality", "cli": "archive",
     "settings": {"mode": "video", "quality": "1080p"}},
    {"name": "Smaller file", "cli": "phone",
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


# ------------------------------------------------------------------- tokens
# Every gap, colour and font the window uses is named here, so a change lands
# in one place instead of in thirty call sites.
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}


# The two label columns the form lines up on, in characters rather than
# pixels: a character follows the system font, and a pixel does not. Each is
# the longest label in its column plus about a character and a half of air --
# "Output:" is 41px of a 52px column, "Sign-in cookies:" 84px of 94px.
LABEL_COL = 8    # the row labels beside the preset and output choices


FIELD_COL = 15   # the field labels inside the More options panel


# Shown whenever the queue is empty. It lives on the status line rather
# than in the queue itself because a table cell is only as wide as its
# column, and widening that column to fit a sentence widened the window.
IDLE_STATUS = "Ready - paste a link above and press Download."


# The surface the widgets sit on belongs to the platform theme, so the text on
# it cannot be a constant: the grey that reads on white fails on the darker
# background Tk's X11 default hands out. These are picked to clear 4.5:1
# against the palest and the darkest surface we can be given.
LIGHT_PALETTE = {
    "field": "#ffffff",
    # The lightest grey that clears 3:1 on both surfaces we are handed:
    # 3.71:1 on Windows #f0f0f0 and 3.03:1 on clam's #dcdad5. SC 1.4.11 wants
    # 3:1 for a control's boundary, and the link box is white on near-white,
    # so this border is the only thing saying where the field is. It was
    # #b0b0b0, which measured 1.90:1 and 1.55:1.
    "border": "#7b7b7b",
    "muted": "#595959",
    "accent": "#0a5fb4",
    # The "Done" green, dark enough to clear the 4.5:1 SC 1.4.3 wants for text
    # on every surface a ttk theme hands us: 5.97:1 on Windows #f0f0f0, 4.87:1
    # on clam's #dcdad5 and 6.80:1 on the white queue field. It was #1a7f37,
    # which measured 4.46:1 and 3.64:1 -- below the minimum on both. Only the
    # lightness moved: hue 136 and saturation 67, against 137 and 66 before.
    "success": "#15692c",
    # Same reasoning as the green, and the same surface decides it: 4.81:1 on
    # clam's #dcdad5, 5.90:1 on Windows #f0f0f0 and 6.72:1 on the white queue
    # field, where "Retrying", "Paused" and "Partial" are drawn. It was
    # #8a5b00, which measured 4.20:1 on clam -- below the 4.5:1 SC 1.4.3 wants
    # for text, while passing everywhere else. Hue 40 and saturation 100 are
    # the amber it always was; only the lightness came down, 27 to 25.
    "warning": "#7e5300",
    "error": "#b42318",
    "warn_bg": "#fdf3c9",
    "on_warn": "#4a3a00",
    "sunken": "#1b1f23",
    "on_sunken": "#c9d1d9",
}


# Reached only where the ttk theme follows the operating system, which today
# means macOS aqua. On Windows (vista) and Linux (clam) the widgets stay light
# whatever the OS is set to, so _is_dark returns False there and this palette
# never applies. Forcing it on regardless would put light-surface greys on a
# light surface -- worse than the honest light window.
#
# Which also means the surface here can only be assumed, so the border is
# picked for a band of aqua dark greys rather than for one of them: #999999
# holds the 3:1 SC 1.4.11 wants for a control boundary on any window from
# #1e1e1e up to #4c4c4c -- 5.85:1 on the #1e1e1e field, 4.50:1 on the nominal
# #323232 window, 5.82:1 on the clip editor's #1b1f23 canvas, and still 3.01:1
# at the light end. It was #555555, which measured 2.24:1 and 1.72:1 and left
# the field with no visible edge at all.
DARK_PALETTE = dict(
    LIGHT_PALETTE, field="#1e1e1e", border="#999999", muted="#a8a8a8",
    accent="#4c9aff", success="#3fb950", warning="#d29922", error="#ff7b72",
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
