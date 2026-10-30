"""The download itself: yt-dlp options, hooks and outcomes."""

import os
import time

from .config import ANSI, SPONSOR_CATEGORIES, STALE_MARKERS, UPDATE_HINT
from .environment import find_ffmpeg
from .text import (
    audio_size_estimate,
    describe_settings,
    format_duration,
    normalise_stage,
    section_suffix,
    stage_label,
)


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
        self.last_info = None   # what the library index records
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
            self._remember(d.get("info_dict"))
            self.on_progress(100, None)

    def _remember(self, info):
        """Keep the few identifying fields the library index wants.

        The whole info dict is megabytes of format listings; holding on to it
        for the sake of four keys would be paid for on every queue item.
        """
        if not info:
            return
        kept = {key: info.get(key) for key in ("id", "title", "ext", "height")}
        if kept.get("height") is None:
            for part in (info.get("requested_formats") or []):
                if part.get("height"):
                    kept["height"] = part["height"]
                    break
        merged = dict(self.last_info or {})
        merged.update({k: v for k, v in kept.items() if v is not None})
        self.last_info = merged

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
            self._remember(d.get("info_dict"))
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
            # Set only when the person was shown what is already there and
            # chose to replace it. Left alone, yt-dlp skips any download whose
            # target name is taken, so an accepted upgrade would report
            # success while the older, smaller file stayed exactly where it was.
            "overwrites": True if settings.get("replace") else None,
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
                                    if stage.get("when") in ("before_dl", "after_filter",
                                                             "pre_process")}
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
        self.last_info = None
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
        """Download a list of links one after another (used by the CLI).

        Returns True only when every link came down whole."""
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
                return False
            failures += 1
            self.on_log("[error] " + message)

        if failures:
            self.on_done(False, "Finished with %d error(s). Anything that did download is in %s"
                         % (failures, out_dir))
            return False
        self.on_done(True, "Done! Saved to " + out_dir)
        return True
