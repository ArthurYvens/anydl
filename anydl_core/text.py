"""Pure formatting and parsing. No I/O, no widgets, no network."""

import os
import re

from .config import PRESETS, STAGE_LABELS


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


def clock_label(seconds, decimals=0):
    """1:30, or 1:02:03 -- the form the two time boxes read back in.

    `decimals` matters more than it looks. A timeline hands back a fractional
    second, and writing 3.79 as "0:03" does not just lose precision: pick two
    marks inside the same second and both boxes end up holding the same text,
    which reads as an end before its start and throws the clip away without
    saying so. parse_timestamp() accepts "0:03.8", so the fraction survives.
    """
    seconds = max(0.0, float(seconds or 0))
    # Split in whole units of the smallest digit shown, never in floats. Round
    # after splitting and 59.7 seconds prints as "0:60", because the rounding
    # happens once the minute has already been decided.
    scale = 10 ** decimals
    units = int(round(seconds * scale))
    hours, rest = divmod(units, 3600 * scale)
    minutes, rest = divmod(rest, 60 * scale)
    secs = rest / float(scale)
    tail = "%0*.*f" % (2 + (decimals + 1 if decimals else 0), decimals, secs)
    if hours:
        return "%d:%02d:%s" % (hours, minutes, tail)
    return "%d:%s" % (minutes, tail)


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


def wanted_height(quality):
    """The quality dropdown as a number, or None for Best."""
    text = str(quality or "").strip().lower()
    if not text or text == "best":
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def describe_existing(entry, settings):
    """What to say about a file that is already there, or None to stay quiet.

    Returns (headline, question). The question is what the person is actually
    being asked, which is not the same in the two cases: a plain duplicate is
    a waste of bandwidth, an upgrade is the reason they came back.
    """
    have_height = entry.get("height")
    have = entry.get("title") or os.path.basename(entry.get("path") or "")
    stamp = entry.get("when")
    shape = "%sp" % have_height if have_height else (entry.get("mode") or "a copy")
    headline = "You already have %s\n  %s, %s%s" % (
        have, shape, os.path.basename(entry.get("path") or ""),
        ", downloaded %s" % stamp if stamp else "")

    want = wanted_height(settings.get("quality"))
    same_mode = entry.get("mode") == settings.get("mode")
    if same_mode and have_height and (want is None or want > have_height):
        return (headline, "This one would be %s. Download the better copy?"
                % ("the best available" if want is None else "%dp" % want))
    if not same_mode:
        return (headline, "You are asking for %s this time. Download it anyway?"
                % (settings.get("mode") or "something else"))
    return (headline, "Download it again anyway?")


def progress_bar(pct, cells=16):
    """A bar drawn in text: a Treeview cell cannot hold a widget.

    Sixteen cells rather than ten. Ten meant a 2 GB file only moved every
    200 MB and read as frozen; going much past sixteen starts dictating how
    wide the whole window has to be.
    """
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * cells))
    return "█" * filled + "░" * (cells - filled)


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
