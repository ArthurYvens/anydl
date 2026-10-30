"""A local proxy of one video, and the stills the timeline scrubs through."""

import os
import shutil
import subprocess
import tempfile
import threading
import time

from .converter import _LoggerAdapter
from .environment import ffmpeg_tool


class ClipSource:
    """A small local copy of one video, plus a grid of stills taken from it.

    Scrubbing a timeline means showing the frame under the handle, and asking
    the remote stream for it costs about three seconds each time: ffmpeg has
    to reach across the network and decode forward from the last keyframe.

    So nothing is scrubbed remotely. One download of the smallest usable
    rendition, one pass that writes a still every couple of seconds, and from
    then on every frame the editor wants is already on disk. Measured on a ten
    minute video: 9s to fetch, 1.5s to write 317 stills, 4ms to put one on
    screen. That last number is the one that makes dragging feel live.
    """

    # Smallest first, and H.264 ahead of VP9 at the same size: VP9 decodes
    # slowly enough to show up in the one seek that is still done per drag.
    PROXY_LADDER = [
        "bestvideo[height<=240][vcodec^=avc1]+bestaudio[ext=m4a]",
        "bestvideo[height<=360][vcodec^=avc1]+bestaudio[ext=m4a]",
        "bestvideo[height<=240]+bestaudio",
        "bestvideo[height<=360]+bestaudio",
        "worst[height>=144]",
        "worst",
    ]
    GRID_WIDTH = 288       # also the size the preview pane shows, so no upscaling
    GRID_TARGET = 320      # stills for the whole video, whatever its length
    GRID_MIN_STEP = 0.5    # but never finer than this, for very short clips

    def __init__(self, url, ffmpeg_dir):
        self.url = url
        self.ffmpeg_dir = ffmpeg_dir
        self.duration = 0.0
        self.title = url
        self.has_audio = False
        self.step = 1.0
        self.frames = []
        self.proxy = None
        self.player = None
        self.cancelled = False
        self._dir = tempfile.mkdtemp(prefix="anydl-clip-")

    # ----------------------------------------------------------------- build
    def prepare(self, on_stage):
        """Fetch the proxy and cut the grid. Raises if either step fails."""
        ffmpeg = ffmpeg_tool("ffmpeg", self.ffmpeg_dir)
        if not ffmpeg:
            raise RuntimeError(
                "Trimming on a timeline needs ffmpeg, and it is not installed. "
                "The two time boxes still work.")
        self._download_proxy(on_stage)
        if self.cancelled:
            return
        self._build_grid(ffmpeg, on_stage)

    def _download_proxy(self, on_stage):
        import yt_dlp

        on_stage("Fetching a small copy to scrub through...", 0.0)
        target = os.path.join(self._dir, "proxy.%(ext)s")

        def hook(status):
            if self.cancelled:
                raise KeyboardInterrupt("closed")
            if status.get("status") != "downloading":
                return
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            done = status.get("downloaded_bytes") or 0
            if total:
                on_stage("Fetching a small copy to scrub through...", done / total)

        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
            "format": "/".join(self.PROXY_LADDER),
            "outtmpl": target, "merge_output_format": "mp4", "overwrites": True,
            "progress_hooks": [hook], "logger": _LoggerAdapter(lambda _msg: None),
            "retries": 3, "fragment_retries": 3,
        }
        if self.ffmpeg_dir:
            opts["ffmpeg_location"] = self.ffmpeg_dir

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.url, download=True)
        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise RuntimeError("There is nothing in that link to trim.")
            info = entries[0]

        self.duration = float(info.get("duration") or 0)
        self.title = info.get("title") or self.url
        if self.duration <= 0:
            raise RuntimeError(
                "This one has no length the site will admit to -- a live stream, "
                "usually. There is nothing to trim against.")

        made = [name for name in os.listdir(self._dir) if name.startswith("proxy.")]
        if not made:
            raise RuntimeError("The small copy did not arrive.")
        self.proxy = os.path.join(self._dir, made[0])
        self.has_audio = self._probe_audio()

    def _probe_audio(self):
        """Whether the proxy carries sound, so Play can say so up front."""
        ffprobe = ffmpeg_tool("ffprobe", self.ffmpeg_dir)
        if not ffprobe or not self.proxy:
            return False
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", self.proxy],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return b"audio" in result.stdout
        except Exception:  # noqa: BLE001
            return False

    def _build_grid(self, ffmpeg, on_stage):
        """One pass over the proxy, writing a still every `step` seconds."""
        on_stage("Cutting the filmstrip...", None)
        self.step = max(self.duration / float(self.GRID_TARGET), self.GRID_MIN_STEP)
        pattern = os.path.join(self._dir, "g%05d.png")
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", self.proxy,
             "-vf", "fps=%.6f,scale=%d:-1" % (1.0 / self.step, self.GRID_WIDTH),
             pattern],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.frames = sorted(os.path.join(self._dir, name)
                             for name in os.listdir(self._dir)
                             if name.startswith("g") and name.endswith(".png"))
        if not self.frames:
            detail = result.stderr.decode("utf8", "replace").strip()[:160]
            raise RuntimeError("No frames came out of the copy. " + detail)

    # ------------------------------------------------------------- accessors
    def frame_for(self, seconds):
        """The grid still nearest that moment. On disk already, so instant."""
        if not self.frames:
            return None
        index = int(round(float(seconds) / self.step))
        return self.frames[max(0, min(index, len(self.frames) - 1))]

    def frame_time(self, seconds):
        """The moment the returned still actually shows, which is rounded to
        the grid. The editor says so rather than implying a precision it does
        not have."""
        index = int(round(float(seconds) / self.step))
        return max(0, min(index, max(len(self.frames) - 1, 0))) * self.step

    def exact_frame(self, seconds):
        """The real frame at that second, decoded on demand.

        About 0.9s, so this is for when a handle is let go, never during the
        drag itself.
        """
        ffmpeg = ffmpeg_tool("ffmpeg", self.ffmpeg_dir)
        if not ffmpeg or not self.proxy:
            return None
        target = os.path.join(self._dir, "exact-%d.png" % int(time.time() * 1000))
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-ss", "%.3f" % max(0.0, seconds),
                 "-i", self.proxy, "-frames:v", "1",
                 "-vf", "scale=%d:-1" % self.GRID_WIDTH, target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return target if result.returncode == 0 and os.path.isfile(target) else None
        except Exception:  # noqa: BLE001
            return None

    # --------------------------------------------------------------- playing
    # Playback targets, and the ceiling that keeps a long clip from turning
    # into thousands of PNGs. Below the floor the result stops reading as
    # motion and starts reading as a slideshow.
    PLAY_FPS = 12
    PLAY_FPS_FLOOR = 6
    PLAY_FRAME_BUDGET = 900

    def preview_frames(self, start, end, on_stage=None):
        """Decode the chosen span to stills the window can page through.

        This is what lets the clip play *inside* the app. Embedding a real
        video surface in Tk means reparenting a foreign window through
        platform APIs, which breaks in ways a download tool should not own --
        but the proxy is already local and small, and one ffmpeg pass over a
        few seconds of it costs about five milliseconds a frame. Paging
        through the result is just PhotoImage, which costs four.

        Returns (paths, fps). The fps comes back because it is lowered for a
        long span, and the caller needs the real one to keep time.
        """
        ffmpeg = ffmpeg_tool("ffmpeg", self.ffmpeg_dir)
        if not ffmpeg or not self.proxy:
            return [], self.PLAY_FPS

        start = max(0.0, start or 0.0)
        end = min(end if end is not None else self.duration, self.duration)
        span = max(0.1, end - start)

        fps = self.PLAY_FPS
        if span * fps > self.PLAY_FRAME_BUDGET:
            fps = max(self.PLAY_FPS_FLOOR, int(self.PLAY_FRAME_BUDGET / span))

        folder = os.path.join(self._dir, "play")
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)
        if on_stage:
            on_stage("Preparing the clip...")

        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", "%.3f" % start,
             "-t", "%.3f" % span, "-i", self.proxy,
             "-vf", "fps=%d,scale=%d:-1" % (fps, self.GRID_WIDTH),
             os.path.join(folder, "p%05d.png")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            return [], fps
        frames = sorted(os.path.join(folder, name) for name in os.listdir(folder)
                        if name.endswith(".png"))
        return frames, fps

    def play_audio(self, start, end, on_started=None):
        """Sound for the span, with no window of its own.

        ffplay draws nothing with -nodisp, so the picture stays in our window
        and this is only the audio track. Returns False when ffplay is not
        installed or the proxy has no sound, and the caller plays silently.

        `on_started` fires when ffplay reports it has opened the input, which
        is the only honest moment to start the picture. Measured here, that
        takes about 870ms and varies by machine, so a constant guess put the
        video most of a second ahead of the sound. It is called from a reader
        thread, so it must not touch a widget.
        """
        ffplay = ffmpeg_tool("ffplay", self.ffmpeg_dir)
        if not ffplay or not self.proxy or not self.has_audio:
            return False
        self.stop()
        span = max(0.1, (end if end is not None else self.duration) - (start or 0.0))
        try:
            self.player = subprocess.Popen(
                [ffplay, "-nodisp", "-hide_banner", "-loglevel", "info", "-autoexit",
                 "-ss", "%.3f" % (start or 0.0), "-t", "%.3f" % span, self.proxy],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                # -nodisp stops ffplay drawing a video window, but on Windows
                # the process still gets a console of its own, which lands on
                # top of the app the moment Play is pressed. Every other
                # subprocess here already passes this flag.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if on_started:
                threading.Thread(target=self._watch_audio_start,
                                 args=(self.player, on_started), daemon=True).start()
            return True
        except Exception:  # noqa: BLE001
            self.player = None
            return False

    @staticmethod
    def _watch_audio_start(process, on_started):
        """Wait for ffplay's first word, then let the caller start the clock.

        Anything it prints means the input is open. If a build says nothing,
        the pipe closes and the caller is released anyway -- never left
        waiting on a line that is not coming.
        """
        try:
            stream = process.stderr
            if stream is not None:
                stream.readline()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                on_started()
            except Exception:  # noqa: BLE001
                pass

    def playing(self):
        return self.player is not None and self.player.poll() is None

    def stop(self):
        if self.player is not None and self.player.poll() is None:
            try:
                self.player.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.player = None

    def close(self):
        self.cancelled = True
        self.stop()
        shutil.rmtree(self._dir, ignore_errors=True)
