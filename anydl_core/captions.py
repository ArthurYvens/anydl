"""Subtitle tracks, and stitching rolling captions into readable lines."""

import json
import re
import urllib.request

from .config import APP_NAME


def _parse_json3(raw):
    """YouTube's json3 caption track into (start, end, text) triples."""
    cues = []
    for event in (json.loads(raw).get("events") or []):
        start = event.get("tStartMs")
        if start is None:
            continue
        text = "".join(seg.get("utf8", "") for seg in (event.get("segs") or []))
        text = " ".join(text.split())
        if text:
            cues.append((start / 1000.0,
                         (start + (event.get("dDurationMs") or 0)) / 1000.0, text))
    return cues


def _parse_vtt(raw):
    """A WebVTT track, for tracks that do not offer json3."""
    stamp = re.compile(r"(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
                       r"(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{3})")

    def seconds(hours, minutes, secs, millis):
        return (int((hours or "0:")[:-1]) * 3600 + int(minutes) * 60
                + int(secs) + int(millis) / 1000.0)

    cues, pending = [], None
    for line in raw.splitlines():
        match = stamp.search(line)
        if match:
            groups = match.groups()
            pending = [seconds(*groups[:4]), seconds(*groups[4:]), []]
            cues.append(pending)
        elif pending is not None and line.strip():
            pending[2].append(re.sub(r"<[^>]+>", "", line).strip())
        elif not line.strip():
            pending = None
    return [(start, end, " ".join(" ".join(parts).split()))
            for start, end, parts in cues if parts]


def tidy_transcript(cues, max_chars=90, max_gap=1.5):
    """Stitch caption fragments into lines a person can read and search.

    Automatic captions arrive as a rolling two or three words per cue, and
    each cue repeats the tail of the one before it. Listing them raw gives
    hundreds of near-identical rows and a search for a phrase matches nothing,
    because the phrase is split across four of them.
    """
    lines = []
    for start, end, text in cues:
        if lines:
            last = lines[-1]
            if text == last["text"] or last["text"].endswith(text):
                last["end"] = max(last["end"], end)
                continue
            joinable = (start - last["end"] <= max_gap
                        and len(last["text"]) + len(text) + 1 <= max_chars)
            if joinable:
                last["text"] = last["text"] + " " + text
                last["end"] = end
                continue
        lines.append({"start": start, "end": end, "text": text})
    return lines


def fetch_transcript(url, prefer=("en",)):
    """The video's captions as readable lines, or [] when it has none.

    Written captions win over machine ones, and the requested languages win
    over whatever happens to sort first, because a search only helps if the
    words are in a language the person actually typed.
    """
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                           "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            return []
        info = entries[0]

    for pool in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        if not pool:
            continue
        languages = [code for code in prefer if code in pool]
        languages += [code for code in pool
                      if any(code.startswith(want) for want in prefer)
                      and code not in languages]
        languages += [code for code in sorted(pool) if code not in languages]
        for code in languages:
            tracks = {track.get("ext"): track for track in pool[code]}
            for ext in ("json3", "vtt"):
                track = tracks.get(ext)
                if not track or not track.get("url"):
                    continue
                try:
                    request = urllib.request.Request(
                        track["url"], headers={"User-Agent": APP_NAME})
                    with urllib.request.urlopen(request, timeout=15) as response:
                        raw = response.read(8 * 1024 * 1024).decode("utf8", "replace")
                    cues = _parse_json3(raw) if ext == "json3" else _parse_vtt(raw)
                except Exception:  # noqa: BLE001
                    continue
                lines = tidy_transcript(cues)
                if lines:
                    return lines
    return []
