"""A local proxy of one video, and the stills the timeline scrubs through."""

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave

from .converter import _LoggerAdapter
from .environment import ffmpeg_tool

try:
    import winsound
except ImportError:  # anywhere but Windows
    winsound = None

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
    # Also the size the monitor shows, fitted inside a box rather than to a
    # width: 16:9 comes back 480x270, 4:3 400x300 and 9:16 169x300, so no
    # shape is taller than the monitor and none is shrunk again on screen.
    # 480 is about the width of the 240p proxy the ladder prefers.
    GRID_BOX = (480, 300)
    SCALE = "scale=%d:%d:force_original_aspect_ratio=decrease" % GRID_BOX
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
        self.sound = None          # the whole soundtrack as a WAV, once cut
        self.ringing = False       # winsound is playing; it has no handle to ask
        self.clips = 0
        self.last_clip = None
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
        # The soundtrack is cut out once, now, alongside the filmstrip. Every
        # ffmpeg costs about a second just to start on Windows, and paying it
        # on each Play was a second of silence after the button was pressed.
        track = os.path.join(self._dir, "sound.wav")
        cutting = subprocess.Popen(
            [ffmpeg, "-y", "-loglevel", "error", "-i", self.proxy, "-vn",
             "-ac", "1", "-ar", str(self.SOUND_RATE), "-c:a", "pcm_s16le", track],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        try:
            self._build_grid(ffmpeg, on_stage)
        finally:
            # A proxy without sound makes ffmpeg fail here, which is how the
            # track's absence is found out; it used to take an ffprobe of its own.
            self.has_audio = cutting.wait() == 0 and os.path.isfile(track)
            self.sound = track if self.has_audio else None

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

    def _build_grid(self, ffmpeg, on_stage):
        """One pass over the proxy, writing a still every `step` seconds."""
        on_stage("Cutting the filmstrip...", None)
        self.step = max(self.duration / float(self.GRID_TARGET), self.GRID_MIN_STEP)
        pattern = os.path.join(self._dir, "g%05d.png")
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", self.proxy,
             "-vf", "fps=%.6f,%s" % (1.0 / self.step, self.SCALE),
             pattern],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW)
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
                 "-vf", self.SCALE, target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=NO_WINDOW)
            return target if result.returncode == 0 and os.path.isfile(target) else None
        except Exception:  # noqa: BLE001
            return None

    # --------------------------------------------------------------- playing
    PLAY_FPS = 12
    SOUND_RATE = 22050     # mono: enough to judge a cut by, a quarter the bytes

    def stream_frames(self, start, end):
        """Decode the span into frames the window takes as they are due.

        This is what lets the clip play *inside* the app. Embedding a real
        video surface in Tk means reparenting a foreign window through
        platform APIs, which breaks in ways a download tool should not own --
        but the proxy is already local and small, and ffmpeg decodes it many
        times faster than it plays. The frames used to be written to PNGs and
        playback waited for the last one; they now arrive through a pipe and
        the first is on screen as soon as ffmpeg has started.

        Returns None without ffmpeg or a proxy.
        """
        ffmpeg = ffmpeg_tool("ffmpeg", self.ffmpeg_dir)
        if not ffmpeg or not self.proxy:
            return None
        start = max(0.0, start or 0.0)
        end = min(end if end is not None else self.duration, self.duration)
        return FrameStream(
            [ffmpeg, "-loglevel", "error", "-ss", "%.3f" % start,
             "-t", "%.3f" % max(0.1, end - start), "-i", self.proxy,
             "-vf", "fps=%d,%s" % (self.PLAY_FPS, self.SCALE),
             "-f", "image2pipe", "-vcodec", "ppm", "pipe:1"])

    def audio_clip(self, start, end):
        """The span's sound as a WAV of its own, cut to the sample.

        ffplay was given the whole proxy and -ss, and it seeks to the keyframe
        *before* that point and plays from there: asked for 0:10, it started at
        5.28 on the test video, so the sound ran seconds ahead of the picture.
        Cutting the samples out of the soundtrack here starts it exactly where
        the clip starts, and costs milliseconds.
        """
        with wave.open(self.sound, "rb") as track:
            rate = track.getframerate()
            track.setpos(min(int(max(0.0, start) * rate), track.getnframes()))
            samples = track.readframes(int(max(0.0, end - start) * rate))
            params = track.getparams()
        # A new name each time: the sound being stopped may still hold the old
        # file open for a moment. The one before it is let go of by now; left
        # alone, every Play added another to the folder until the window shut.
        if self.last_clip:
            try:
                os.remove(self.last_clip)
            except OSError:
                pass
        self.clips += 1
        path = os.path.join(self._dir, "clip%d.wav" % self.clips)
        with wave.open(path, "wb") as out:
            out.setparams(params)
            out.writeframes(samples)
        self.last_clip = path
        return path

    def can_sound(self):
        """Whether Play will be heard: a soundtrack, and something to play it."""
        return bool(self.sound) and (winsound is not None
                                     or bool(ffmpeg_tool("ffplay", self.ffmpeg_dir)))

    def play_audio(self, start, end, on_started=None):
        """Sound for the span, with no window of its own.

        `on_started` fires when the sound is actually going, which is the
        moment to start the picture's clock. On Windows that is before this
        returns: winsound starts in a few milliseconds. Elsewhere ffplay takes
        most of a second to open, and the callback comes from a reader thread
        once it has, so it must not touch a widget.

        Returns False when there is nothing to play it with; the caller plays
        silently.
        """
        if not self.can_sound():
            return False
        self.stop()
        try:
            clip = self.audio_clip(start, end)
        except (OSError, EOFError, wave.Error):
            return False
        if winsound is not None:
            winsound.PlaySound(clip, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
            self.ringing = True
            if on_started:
                on_started()
            return True
        try:
            self.player = subprocess.Popen(
                [ffmpeg_tool("ffplay", self.ffmpeg_dir), "-nodisp", "-hide_banner",
                 "-loglevel", "info", "-autoexit", clip],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except Exception:  # noqa: BLE001
            self.player = None
            return False
        threading.Thread(target=self._watch_audio_start,
                         args=(self.player, on_started), daemon=True).start()
        return True

    @staticmethod
    def _watch_audio_start(process, on_started):
        """Wait for ffplay's first word, release the caller, then keep reading.

        Anything it prints means the input is open. It goes on printing a
        status line every 30ms after that, and a pipe nobody reads fills up
        and stops ffplay mid-clip -- so the rest is read and thrown away. If a
        build says nothing, the pipe closes and the caller is released anyway.
        """
        stream = process.stderr
        try:
            if stream is not None:
                stream.readline()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                if on_started:
                    on_started()
            except Exception:  # noqa: BLE001
                pass
        try:
            while stream is not None and stream.read(4096):
                pass
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        if self.ringing and winsound is not None:
            winsound.PlaySound(None, 0)
        self.ringing = False
        if self.player is not None and self.player.poll() is None:
            try:
                self.player.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.player = None

    def close(self):
        self.cancelled = True
        self.stop()
        self.discard()

    def discard(self):
        shutil.rmtree(self._dir, ignore_errors=True)


class FrameStream:
    """Frames from one ffmpeg, read off its pipe as PPM.

    PPM because Tk reads it straight from bytes, in about a millisecond for a
    480x270 frame, with no file and no zlib in between. The queue is bounded:
    ffmpeg decodes far faster than the clip plays, and unbounded it would hold
    the whole span in memory at 390KB a frame. Full, it makes the reader wait,
    which makes ffmpeg wait.
    """

    AHEAD = 36   # three seconds at 12fps

    def __init__(self, command):
        self.frames = queue.Queue(maxsize=self.AHEAD)
        self.taken = 0          # how many the window has pulled off the queue
        self.finished = False   # ffmpeg is done and the reader has stopped
        self.closed = False
        self.process = None
        threading.Thread(target=self._read, args=(command,), daemon=True).start()

    def _read(self, command):
        try:
            # Started here, not in __init__: on Windows creating the process
            # alone took about a second, measured on the window's own thread,
            # and the sound waited for it.
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL,
                                            creationflags=NO_WINDOW)
            if self.closed:  # stopped while it was starting
                self.process.kill()
            while not self.closed:
                frame = read_ppm(self.process.stdout)
                if frame is None:
                    break
                while not self.closed:
                    try:
                        self.frames.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass  # closed under the reader
        finally:
            self.finished = True

    def ready(self):
        """Whether a frame is waiting, or none ever will."""
        return not self.frames.empty() or self.finished

    def take(self, index):
        """The newest frame up to `index`, or None if none has arrived.

        Frames before it are dropped: a frame that arrives late is not shown
        late, it is skipped, so the picture never falls behind the sound.
        """
        newest = None
        while self.taken <= index:
            try:
                newest = self.frames.get_nowait()
            except queue.Empty:
                break
            self.taken += 1
        return newest

    def close(self):
        self.closed = True
        try:
            if self.process is not None:
                self.process.kill()
        except OSError:
            pass


def read_ppm(stream):
    """One binary PPM off a pipe, header included, or None at the end."""
    magic = stream.readline()
    if not magic:
        return None
    size = stream.readline()
    depth = stream.readline()
    width, height = (int(part) for part in size.split())
    body = stream.read(width * height * 3)
    if len(body) < width * height * 3:
        return None
    return magic + size + depth + body
