"""What has already been downloaded into a folder."""

import json
import os
import time


class LibraryIndex:
    """A record of what has already been downloaded into one folder.

    yt-dlp's own answer to this is --download-archive, which only knows yes or
    no: an id is in the list or it is not. That refuses to re-fetch the 720p
    copy you regret, which is the one case where you do want to fetch again.

    So this keeps what was actually taken -- the height, the file, the day --
    and the window can say "you have this at 720p, this would be 1080p" and
    let the person decide, instead of either silently skipping or silently
    writing a second copy beside the first.

    It lives in the output folder rather than in a config directory, so it
    travels with the files it describes and a different folder has its own
    history. Nothing here may ever stop a download: every failure is swallowed
    and the feature simply goes quiet.
    """

    FILENAME = ".anydl-library.json"

    def __init__(self, out_dir):
        self.path = os.path.join(out_dir, self.FILENAME)
        self.entries = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.entries = data.get("entries") or {}
        except Exception:  # noqa: BLE001
            self.entries = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf8") as handle:
                json.dump({"version": 1, "entries": self.entries}, handle, indent=1)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def key_for(url, video_id=None):
        """Prefer the site's id; fall back to a tidied URL.

        The id is stable across the dozen shapes the same YouTube link takes.
        The URL is what is known before any network call, which is when the
        check has to happen if it is going to be worth anything.
        """
        if video_id:
            return "id:" + str(video_id)
        tidy = (url or "").strip().split("#")[0]
        for prefix in ("https://", "http://", "www."):
            if tidy.startswith(prefix):
                tidy = tidy[len(prefix):]
        return "url:" + tidy.rstrip("/").lower()

    def find(self, url, video_id=None):
        for key in (self.key_for(url, video_id), self.key_for(url)):
            entry = self.entries.get(key)
            if entry and os.path.isfile(entry.get("path") or ""):
                return entry
            if entry:
                # The file was moved or deleted; the record is stale, so drop
                # it rather than warning about something that is not there.
                self.entries.pop(key, None)
        return None

    def record(self, url, info, path, settings):
        info = info or {}
        entry = {
            "title": info.get("title") or os.path.basename(path or ""),
            "path": path or "",
            "height": info.get("height"),
            "ext": info.get("ext"),
            "mode": settings.get("mode"),
            "quality": settings.get("quality"),
            "when": time.strftime("%Y-%m-%d"),
            "url": url,
        }
        self.entries[self.key_for(url, info.get("id"))] = entry
        if info.get("id"):
            # Also under the URL, so the next check finds it before the first
            # network call has happened.
            self.entries[self.key_for(url)] = entry
        self.save()
        return entry
