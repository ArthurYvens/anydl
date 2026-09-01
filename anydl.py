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
def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    window = tk.Tk()
    window.title(APP_NAME + " - video and audio downloader")
    window.geometry("780x600")
    window.minsize(700, 540)

    events = queue.Queue()
    state = {"running": False, "converter": None}
    fallback_dir = default_output_dir()

    top = ttk.Frame(window, padding=12)
    top.pack(fill="x")

    ttk.Label(top, text="Link(s), one per line:").pack(anchor="w")
    urls_box = tk.Text(top, height=4, wrap="none")
    urls_box.pack(fill="x", pady=(4, 10))

    choices = ttk.Frame(top)
    choices.pack(fill="x")

    mode_var = tk.StringVar(value="video")
    ttk.Radiobutton(choices, text="Video (MP4)", value="video",
                    variable=mode_var).grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(choices, text="Audio only", value="audio",
                    variable=mode_var).grid(row=0, column=1, sticky="w", padx=(16, 0))

    playlist_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(choices, text="Download the whole playlist",
                    variable=playlist_var).grid(row=0, column=2, sticky="w", padx=(24, 0))

    row2 = ttk.Frame(top)
    row2.pack(fill="x", pady=(8, 0))

    ttk.Label(row2, text="Quality:").grid(row=0, column=0, sticky="w")
    quality_box = ttk.Combobox(row2, values=VIDEO_QUALITIES, width=10, state="readonly")
    quality_box.set("Best")
    quality_box.grid(row=0, column=1, padx=(6, 20))

    ttk.Label(row2, text="Audio format:").grid(row=0, column=2, sticky="w")
    format_box = ttk.Combobox(row2, values=AUDIO_FORMATS, width=8, state="readonly")
    format_box.set("mp3")
    format_box.grid(row=0, column=3, padx=(6, 20))

    ttk.Label(row2, text="Bitrate:").grid(row=0, column=4, sticky="w")
    bitrate_box = ttk.Combobox(row2, values=AUDIO_BITRATES, width=6, state="readonly")
    bitrate_box.set("192")
    bitrate_box.grid(row=0, column=5, padx=(6, 0))

    row_login = ttk.Frame(top)
    row_login.pack(fill="x", pady=(8, 0))

    ttk.Label(row_login, text="Sign-in cookies:").pack(side="left")
    browser_box = ttk.Combobox(row_login, values=BROWSERS, width=10, state="readonly")
    browser_box.set("none")
    browser_box.pack(side="left", padx=(6, 8))
    ttk.Label(
        row_login,
        text="borrow a logged-in session for sites like Instagram or Vimeo",
        foreground="#777777",
    ).pack(side="left")

    cookie_file_var = tk.StringVar(value="")

    def pick_cookie_file():
        chosen = filedialog.askopenfilename(
            title="Select a cookies.txt file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        cookie_file_var.set(chosen or "")
        if chosen:
            write("[cookies] file selected: " + os.path.basename(chosen))
            browser_box.set("none")

    ttk.Button(row_login, text="cookies.txt...",
               command=pick_cookie_file).pack(side="right")

    row3 = ttk.Frame(top)
    row3.pack(fill="x", pady=(10, 0))
    ttk.Label(row3, text="Save to:").pack(side="left")
    dest_var = tk.StringVar(value=fallback_dir)
    ttk.Entry(row3, textvariable=dest_var).pack(side="left", fill="x", expand=True, padx=6)

    def choose_folder():
        chosen = filedialog.askdirectory(initialdir=dest_var.get() or fallback_dir)
        if chosen:
            dest_var.set(chosen)

    ttk.Button(row3, text="...", width=4, command=choose_folder).pack(side="left")

    def open_folder():
        target = dest_var.get()
        if os.path.isdir(target):
            open_in_file_manager(target)
        else:
            messagebox.showinfo(APP_NAME, "That folder does not exist yet.")

    ttk.Button(row3, text="Open folder", command=open_folder).pack(side="left", padx=(6, 0))

    buttons = ttk.Frame(top)
    buttons.pack(fill="x", pady=(12, 0))
    download_btn = ttk.Button(buttons, text="Download")
    download_btn.pack(side="left")
    cancel_btn = ttk.Button(buttons, text="Cancel", state="disabled")
    cancel_btn.pack(side="left", padx=(8, 0))
    clear_btn = ttk.Button(buttons, text="Clear log")
    clear_btn.pack(side="left", padx=(8, 0))

    body = ttk.Frame(window, padding=(12, 0, 12, 12))
    body.pack(fill="both", expand=True)

    bar = ttk.Progressbar(body, mode="determinate", maximum=100)
    bar.pack(fill="x", pady=(8, 4))
    status_var = tk.StringVar(value="Ready.")
    ttk.Label(body, textvariable=status_var).pack(anchor="w")

    log_frame = ttk.Frame(body)
    log_frame.pack(fill="both", expand=True, pady=(8, 0))
    scrollbar = ttk.Scrollbar(log_frame)
    scrollbar.pack(side="right", fill="y")
    log_box = tk.Text(log_frame, height=12, wrap="word", state="disabled",
                      background="#111111", foreground="#dddddd",
                      insertbackground="#dddddd", yscrollcommand=scrollbar.set)
    log_box.pack(side="left", fill="both", expand=True)
    scrollbar.configure(command=log_box.yview)

    def write(text):
        log_box.configure(state="normal")
        log_box.insert("end", text + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    # Worker threads never touch Tk directly; they post events onto the queue.
    def on_log(msg):
        events.put(("log", str(msg)))

    def on_progress(pct, text):
        events.put(("progress", (pct, text)))

    def on_done(ok, msg):
        events.put(("done", (ok, msg)))

    def pump():
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "log":
                    write(payload)
                elif kind == "progress":
                    pct, text = payload
                    bar["value"] = pct
                    status_var.set(text)
                elif kind == "done":
                    ok, msg = payload
                    state["running"] = False
                    download_btn.configure(state="normal")
                    cancel_btn.configure(state="disabled")
                    bar["value"] = 100 if ok else 0
                    status_var.set(msg)
                    write("\n" + msg)
        except queue.Empty:
            pass
        window.after(120, pump)

    def start():
        if state["running"]:
            return
        raw = urls_box.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning(APP_NAME, "Paste at least one link.")
            return
        out_dir = dest_var.get().strip() or fallback_dir
        dest_var.set(out_dir)

        state["running"] = True
        download_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")
        bar["value"] = 0
        status_var.set("Starting...")

        converter = Converter(
            on_log=on_log, on_progress=on_progress, on_done=on_done,
            browser=browser_box.get(), cookie_file=cookie_file_var.get() or None,
        )
        state["converter"] = converter

        threading.Thread(
            target=converter.download,
            args=(urls, out_dir, mode_var.get(), quality_box.get(),
                  format_box.get(), bitrate_box.get(), playlist_var.get()),
            daemon=True,
        ).start()

    def cancel():
        converter = state.get("converter")
        if converter:
            converter.cancelled = True
            status_var.set("Cancelling...")

    def clear_log():
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.configure(state="disabled")
        bar["value"] = 0
        status_var.set("Ready.")

    download_btn.configure(command=start)
    cancel_btn.configure(command=cancel)
    clear_btn.configure(command=clear_log)

    def sync_fields(*_args):
        audio = mode_var.get() == "audio"
        quality_box.configure(state="disabled" if audio else "readonly")
        format_box.configure(state="readonly" if audio else "disabled")
        bitrate_box.configure(state="readonly" if audio else "disabled")

    mode_var.trace_add("write", sync_fields)
    sync_fields()

    if find_ffmpeg():
        write("ffmpeg detected.")
    else:
        write("[warning] ffmpeg not found: MP3 output and 1080p+ are unavailable. "
              "See the README for how to install it.")

    pump()
    window.mainloop()


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
