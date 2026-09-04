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
import queue
import shutil
import argparse
import threading
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(APP_DIR, "bin")

APP_NAME = "anydl"
VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
AUDIO_FORMATS = ["mp3", "m4a", "wav", "opus", "flac"]
AUDIO_BITRATES = ["320", "256", "192", "160", "128", "96"]

# Sites like Instagram, Vimeo or a private playlist only answer to a logged-in
# session. yt-dlp can borrow one from a local browser profile.
BROWSERS = ["none", "firefox", "chrome", "edge", "brave", "chromium",
            "opera", "vivaldi", "safari", "whale"]

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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
                 browser=None, cookie_file=None):
        self.on_log = on_log
        self.on_progress = on_progress or (lambda pct, text: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self.cancelled = False
        self.ffmpeg_dir = find_ffmpeg()
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
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100) if total else 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            self.on_progress(
                pct,
                "%5.1f%%  %.1f/%.1f MB  %.2f MB/s  ETA %ss"
                % (pct, done / 1048576, total / 1048576, speed / 1048576, eta),
            )
        elif status == "finished":
            self.on_progress(100, "download finished, processing...")

    def _postprocessor_hook(self, d):
        if d.get("status") == "started":
            self.on_log("[convert] " + str(d.get("postprocessor", "")))

    # ---------------------------------------------------------------- options
    def _build_options(self, out_dir, mode, quality, audio_format, bitrate, playlist):
        if playlist:
            template = os.path.join(out_dir, "%(playlist_title)s", "%(title)s.%(ext)s")
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

        if mode == "audio":
            self._audio_options(opts, audio_format, bitrate)
        else:
            self._video_options(opts, quality)
        return opts

    def _audio_options(self, opts, audio_format, bitrate):
        if not self.ffmpeg_dir:
            # No ffmpeg means no transcoding: keep the original stream as-is.
            opts["format"] = "bestaudio[ext=m4a]/bestaudio"
            self.on_log("[warning] ffmpeg missing: saving the original audio without converting.")
            return

        opts["format"] = "bestaudio/best"
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": bitrate,
            },
            {"key": "FFmpegMetadata"},
        ]
        if audio_format in ("mp3", "m4a", "flac"):
            opts["writethumbnail"] = True
            postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        opts["postprocessors"] = postprocessors

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
        opts["postprocessors"] = [{"key": "FFmpegMetadata"}]

    # ----------------------------------------------------------------- public
    def probe(self, url):
        """Fetch metadata for a single URL without downloading anything."""
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        self._apply_cookies(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def download(self, urls, out_dir, mode="video", quality="Best",
                 audio_format="mp3", bitrate="192", playlist=False):
        try:
            import yt_dlp
        except ImportError:
            self.on_done(False, "yt-dlp is not installed. Run: pip install -U yt-dlp")
            return

        self.cancelled = False
        os.makedirs(out_dir, exist_ok=True)
        opts = self._build_options(out_dir, mode, quality, audio_format, bitrate, playlist)
        failures = 0

        if self.browser:
            self.on_log("[cookies] using the signed-in session from " + self.browser)
        elif self.cookie_file:
            self.on_log("[cookies] using " + os.path.basename(self.cookie_file))

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                for url in urls:
                    if self.cancelled:
                        break
                    self.on_log("\n>> " + url)
                    try:
                        ydl.download([url])
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        self.on_log("[error] " + self._explain(exc))
        except KeyboardInterrupt:
            self.on_done(False, "Cancelled.")
            return
        except Exception as exc:  # noqa: BLE001
            self.on_done(False, "Failed: " + self._explain(exc))
            return

        if failures:
            self.on_done(True, "Finished with %d error(s). Saved to %s" % (failures, out_dir))
        else:
            self.on_done(True, "Done! Saved to " + out_dir)


# ====================================================================== GUI
# tkinter is imported on demand. Several Linux distributions package it apart
# from Python, and the command line mode has to keep working without it.
tk = ttk = filedialog = messagebox = None


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


class AnydlApp:
    """The window.

    Downloads run on a worker thread, and a worker thread must never touch a Tk
    widget. Workers publish events onto `self.events` instead, and `pump()`
    drains that queue on the main thread every 120 ms.
    """

    def __init__(self):
        self.events = queue.Queue()
        self.fallback_dir = default_output_dir()
        self.running = False
        self.converter = None

        self.window = tk.Tk()
        self.window.title(APP_NAME + " - video and audio downloader")
        self.window.geometry("780x600")
        self.window.minsize(700, 540)
        apply_window_icon(self.window)

        top = ttk.Frame(self.window, padding=12)
        top.pack(fill="x")
        self._build_links(top)
        self._build_choices(top)
        self._build_login(top)
        self._build_destination(top)
        self._build_buttons(top)

        body = ttk.Frame(self.window, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        self._build_progress(body)
        self._build_log(body)

        self.mode_var.trace_add("write", self.sync_fields)
        self.sync_fields()

        if find_ffmpeg():
            self.log("ffmpeg detected.")
        else:
            self.log("[warning] ffmpeg not found: MP3 output and 1080p+ are unavailable. "
                     "See the README for how to install it.")

    def run(self):
        self.pump()
        self.window.mainloop()

    # ------------------------------------------------------------------ build
    def _build_links(self, parent):
        ttk.Label(parent, text="Link(s), one per line:").pack(anchor="w")
        self.urls_box = tk.Text(parent, height=4, wrap="none")
        self.urls_box.pack(fill="x", pady=(4, 10))

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
        self.cancel_btn = ttk.Button(row, text="Cancel", state="disabled", command=self.cancel)
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Clear log", command=self.clear_log).pack(side="left", padx=(8, 0))

    def _build_progress(self, parent):
        self.bar = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(8, 4))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(parent, textvariable=self.status_var).pack(anchor="w")

    def _build_log(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=(8, 0))
        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        self.log_box = tk.Text(frame, height=12, wrap="word", state="disabled",
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
        self.bar["value"] = 0
        self.status_var.set("Ready.")

    def sync_fields(self, *_args):
        audio = self.mode_var.get() == "audio"
        self.quality_box.configure(state="disabled" if audio else "readonly")
        self.format_box.configure(state="readonly" if audio else "disabled")
        self.bitrate_box.configure(state="readonly" if audio else "disabled")

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

    # ----------------------------------------------------------------- events
    def pump(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    pct, text = payload
                    self.bar["value"] = pct
                    self.status_var.set(text)
                elif kind == "done":
                    ok, msg = payload
                    self.running = False
                    self.download_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.bar["value"] = 100 if ok else 0
                    self.status_var.set(msg)
                    self.log("\n" + msg)
        except queue.Empty:
            pass
        self.window.after(120, self.pump)

    # -------------------------------------------------------------- downloads
    def start(self):
        if self.running:
            return
        raw = self.urls_box.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning(APP_NAME, "Paste at least one link.")
            return
        out_dir = self.dest_var.get().strip() or self.fallback_dir
        self.dest_var.set(out_dir)

        self.running = True
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.bar["value"] = 0
        self.status_var.set("Starting...")

        self.converter = Converter(
            on_log=lambda msg: self.events.put(("log", str(msg))),
            on_progress=lambda pct, text: self.events.put(("progress", (pct, text))),
            on_done=lambda ok, msg: self.events.put(("done", (ok, msg))),
            browser=self.browser_var.get(),
            cookie_file=self.cookie_file_var.get() or None,
        )

        threading.Thread(
            target=self.converter.download,
            args=(urls, out_dir, self.mode_var.get(), self.quality_var.get(),
                  self.format_var.get(), self.bitrate_var.get(), self.playlist_var.get()),
            daemon=True,
        ).start()

    def cancel(self):
        if self.converter:
            self.converter.cancelled = True
            self.status_var.set("Cancelling...")


def launch_gui():
    claim_taskbar_identity()  # must happen before the first window exists
    _load_tk()
    AnydlApp().run()


# ====================================================================== CLI
def main():
    parser = argparse.ArgumentParser(
        prog="anydl",
        description="Download video as MP4 from any site yt-dlp supports, "
                    "or extract just the audio track.",
    )
    parser.add_argument("url", nargs="*", help="link(s) to download")
    parser.add_argument("-a", "--audio", action="store_true", help="extract audio only")
    parser.add_argument("-f", "--format", default="mp3", choices=AUDIO_FORMATS,
                        dest="audio_format", help="audio format (default: mp3)")
    parser.add_argument("-b", "--bitrate", default="192", help="audio bitrate in kbps")
    parser.add_argument("-q", "--quality", default="Best",
                        help="video quality: Best, 1080p, 720p, ...")
    parser.add_argument("-o", "--output", default=default_output_dir(),
                        help="destination folder")
    parser.add_argument("--playlist", action="store_true", help="download the whole playlist")
    parser.add_argument("--cookies-from-browser", dest="browser", choices=BROWSERS,
                        default="none",
                        help="borrow the signed-in session from a local browser "
                             "(needed for Instagram, Vimeo and other gated sites)")
    parser.add_argument("--cookies", dest="cookie_file", default=None,
                        help="path to a cookies.txt file, as an alternative to "
                             "--cookies-from-browser")
    args = parser.parse_args()

    if not args.url:
        launch_gui()
        return

    last = [-1]

    def on_progress(pct, text):
        if int(pct) != last[0]:
            last[0] = int(pct)
            print("\r" + text.ljust(70), end="", flush=True)

    converter = Converter(
        on_log=lambda m: print("\n" + str(m)),
        on_progress=on_progress,
        on_done=lambda ok, m: print("\n" + m),
        browser=args.browser,
        cookie_file=args.cookie_file,
    )
    converter.download(args.url, args.output, "audio" if args.audio else "video",
                       args.quality, args.audio_format, args.bitrate, args.playlist)


if __name__ == "__main__":
    main()
