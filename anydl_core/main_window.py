"""The main window and its download queue."""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import (
    APP_NAME,
    AUDIO_BITRATES,
    AUDIO_FORMATS,
    BROWSERS,
    CUSTOM_PRESET,
    DARK_PALETTE,
    EMPTY_ROW,
    FIELD_COL,
    IDLE_STATUS,
    LABEL_COL,
    PRESETS,
    STALL_SECONDS,
    STATUS_ORDER,
    STATUS_TABLE,
    SUBTITLE_LANGS,
    UPDATE_HINT,
    VIDEO_QUALITIES,
)
from .converter import Converter
from .environment import (
    apply_window_icon,
    claim_taskbar_identity,
    default_output_dir,
    fetch_thumbnail,
    find_ffmpeg,
    installed_ytdlp_version,
    latest_ytdlp_version,
    open_in_file_manager,
    probe_height,
    update_ytdlp,
)
from .library import LibraryIndex
from .text import (
    clock_label,
    describe_existing,
    describe_settings,
    find_preset,
    format_date,
    format_duration,
    format_progress,
    format_size,
    parse_timestamp,
    parse_version,
    progress_bar,
    stage_label,
)
from .theme import Theme
from .trim_window import ClipEditor


class QueueItem:
    """One line of the queue, holding the settings as they were when it was
    added: a queue can mix an MP3 and a 1080p video, and changing a dropdown
    afterwards does not rewrite what is already waiting."""

    def __init__(self, uid, url, settings, label=None):
        self.uid = uid
        self.url = url
        self.settings = settings
        self.label = label or url  # replaced by the real title once known
        self.status = "Queued"
        self.progress = 0.0
        self.detail = ""
        self.note = ""  # the whole failure text, not just the first line
        self.cancelled = False
        self.pause_requested = False
        self.converter = None
        self.last_event = time.monotonic()

    @property
    def finished(self):
        return STATUS_TABLE[self.status]["over"]

    @property
    def busy(self):
        return STATUS_TABLE[self.status]["busy"]

    @property
    def summary(self):
        """The settings this row carries, short enough for a column.

        Two rows in the same queue can be doing entirely different things, and
        until this existed there was no way to tell them apart.
        """
        settings = self.settings
        if settings.get("mode") == "audio":
            parts = [settings.get("audio_format", "mp3").upper(),
                     settings.get("bitrate", "192")]
        else:
            parts = ["MP4", settings.get("quality", "Best")]
        if settings.get("playlist"):
            parts.append("playlist")
        if settings.get("subtitles", "none") != "none":
            parts.append("sub " + settings["subtitles"])
        if settings.get("sponsorblock"):
            parts.append("no sponsors")
        if settings.get("section_start") is not None or settings.get("section_end") is not None:
            parts.append("clip")
        return " · ".join(parts)


class AnydlApp:
    """The window.

    Downloads run on worker threads, and a worker must never touch a Tk widget.
    They publish onto `self.events`, and `pump()` drains it on the main thread.
    """

    def __init__(self):
        self.events = queue.Queue()
        self.fallback_dir = default_output_dir()
        self.ffmpeg_dir = find_ffmpeg()
        # The queue: `items` is the worker's view (guarded by the lock), `rows`
        # is the window's, keyed by the same uid the Treeview uses for its row.
        self.items = []
        self.rows = {}
        self.lock = threading.Lock()
        self.worker_running = False
        self.next_uid = 0
        self.hint = UPDATE_HINT
        self.rate_limit = None
        self.last_clipboard = ""
        self.applying_preset = False
        self.checking = False
        self.installed_version = None
        self.latest_version = None
        # None until the check comes back; False only once we know it is gone.
        self.engine_ok = None
        self.extras_open = False
        self.bar_mode = "determinate"
        self.ready = False  # set once the window has been sized
        self.base_title = APP_NAME + " - video and audio downloader"

        self.window = tk.Tk()
        self.window.title(self.base_title)
        self.window.minsize(700, 500)  # replaced by _fit_to_screen once built
        apply_window_icon(self.window)
        self.theme = Theme(self.window)

        # The banner is built first so keyboard focus reaches it first: Tk
        # traverses in creation order, and pack(before=...) moves the pixels
        # without moving the tab stop. It carries the one message that can
        # explain why nothing else in the window works.
        self._build_banner(self.window)

        self.top = ttk.Frame(self.window, padding=self.theme.px("md"))
        self.top.pack(fill="x")
        self._build_links(self.top)
        self._build_message(self.top)
        # More air above a rule than below it: a separator opens the group
        # under it, and 12px on both sides left it floating between the two.
        ttk.Separator(self.top).pack(fill="x", pady=self.theme.pad("md", "sm"))
        self._build_presets(self.top)
        self._build_choices(self.top)
        self._build_disclosure(self.top)
        self._build_extras(self.top)
        ttk.Separator(self.top).pack(fill="x", pady=self.theme.pad("md", "sm"))
        self._build_destination(self.top)
        self._build_buttons(self.top)

        # Creation order is tab order and pack order is layout; the two are
        # independent. The body is built first so it is reached before the
        # footer, and packed last so the footer keeps its strip at the bottom.
        body = ttk.Frame(self.window, padding=(self.theme.px("md"), 0,
                                               self.theme.px("md"), self.theme.px("md")))
        self._build_progress(body)
        split = ttk.PanedWindow(body, orient="vertical")
        self._build_queue(split)
        self._build_log(split)
        self._build_footer(self.window)
        body.pack(fill="both", expand=True)
        # "xs" above, not "sm": the status line belongs to the queue under it,
        # and every pixel spent here is a pixel the two panes do not get on a
        # screen that has none to spare.
        split.pack(fill="both", expand=True, pady=self.theme.pad("xs", "xs"))

        self.setting_vars = {
            "mode": self.mode_var,
            "quality": self.quality_var,
            "audio_format": self.format_var,
            "bitrate": self.bitrate_var,
        }
        for variable in self.setting_vars.values():
            variable.trace_add("write", self.on_manual_change)
        self.mode_var.trace_add("write", self.sync_fields)
        for variable in (self.playlist_var, self.subs_var, self.sponsor_var,
                         self.section_start_var, self.section_end_var,
                         self.browser_var, self.cookie_file_var):
            variable.trace_add("write", self.refresh_disclosure)
        self.sync_fields()
        # apply_preset, not describe_preset: the opening preset has to push its
        # settings into the controls. It sets applying_preset, so this does not
        # read as a manual change and flip the choice to Custom.
        self.apply_preset()
        self.show_empty_row()
        self._bind_keys()
        # Shut first, then measure: the minimum has to describe how the window
        # actually opens. Opening the panel later grows the window instead of
        # having reserved its height from the start.
        self.toggle_extras()
        self._fit_to_screen()
        self.ready = True

        self.urls_box.focus_set()
        if self.ffmpeg_dir:
            self.log("ffmpeg detected.")
        else:
            self.log("[warning] ffmpeg not found: MP3 output and 1080p+ are unavailable. "
                     "See the README for how to install it.")
            self.show_banner(
                "ffmpeg is not installed. Audio is saved as it comes instead of converted, "
                "video stops near 720p, and subtitles and SponsorBlock are unavailable.",
                None)

    def _fit_to_screen(self):
        """Size the window from what the widgets need, not from the screen.

        Everything above the queue has a fixed height, so a short window
        squeezes the queue and the log -- to nothing at all, in the worst case.
        The required size becomes the minimum; a taller screen buys them more
        room. (Under WSLg the reported screen is 640x480, which is how the two
        panes came to vanish entirely.)
        """
        self.window.update_idletasks()
        needed = self.window.winfo_reqheight()
        # Width has to come from the widgets too: at a larger system font the
        # button row outgrows any constant, and pack answers that by quietly
        # dropping whatever it cannot fit.
        wide = max(700, self.window.winfo_reqwidth())
        # Never demand more height than the screen has. The queue and the log
        # can give ground -- they sit in a paned window for exactly that
        # reason -- but a minimum taller than the display is a window that
        # cannot be opened at all.
        #
        # What the allowance has to cover is the title bar and the taskbar,
        # which is about 80px at 100% and grows with the display, so it is
        # scaled rather than fixed. It was a flat 160, and those 40 spare
        # pixels were the difference between three queue rows and none on a
        # 1366x768 laptop, where this term is the one that binds.
        ceiling = max(480, self.window.winfo_screenheight() - self.theme.scaled(120))
        # The same applies sideways. A minimum wider than the display is a
        # window that cannot be narrowed to fit the screen it is on.
        wide = min(wide, max(640, self.window.winfo_screenwidth() - self.theme.scaled(80)))
        self.window.minsize(wide, min(needed, ceiling))
        # Opening size, and where to put it. Tk's own placement had the
        # window starting a third of the way down a 1080px screen at 960px
        # tall, which put the footer under the bottom edge.
        screen_h = self.window.winfo_screenheight()
        screen_w = self.window.winfo_screenwidth()
        high = max(min(needed, ceiling),
                   min(screen_h - self.theme.scaled(200), needed + self.theme.scaled(120)))
        wide = min(wide + self.theme.scaled(40), screen_w - self.theme.scaled(80))
        self.window.geometry("%dx%d+%d+%d" % (
            wide, high, max(0, (screen_w - wide) // 2), max(0, (screen_h - high) // 3)))

    def run(self):
        self.pump()
        self.poll_clipboard()
        self.check_for_updates()  # in the background; the window is already up
        self.window.mainloop()

    # ------------------------------------------------------------------ build
    def _build_banner(self, parent):
        """A strip that stays hidden until something needs attention."""
        self.banner = ttk.Frame(parent, style="Banner.TFrame")
        inner = ttk.Frame(self.banner, style="Banner.TFrame",
                          padding=self.theme.pad("md", "sm", "md", "sm"))
        inner.pack(fill="x")

        self.banner_var = tk.StringVar(value="")
        # 640, not 520: the update notice measures 538px in the body font, so
        # the narrower wrap pushed its last word onto a second line while
        # 666px of the strip sat empty beside the two buttons.
        ttk.Label(inner, textvariable=self.banner_var, style="Banner.TLabel",
                  anchor="w", justify="left", wraplength=self.theme.scaled(640)).pack(side="left",
                                                                   fill="x", expand=True)
        # Built before the dismiss button so the tab order matches what the eye
        # sees: packing to the right puts the first one packed furthest right.
        self.update_btn = ttk.Button(inner, text="Update now", command=self.start_update)
        self.dismiss_btn = ttk.Button(inner, text="Later", command=self.hide_banner)
        self.update_btn.pack(side="right", padx=self.theme.pad("sm", "sm"))
        self.dismiss_btn.pack(side="right")

    def _build_footer(self, parent):
        footer = ttk.Frame(parent, padding=(self.theme.px("md"), 0,
                                            self.theme.px("md"), self.theme.px("sm")))
        footer.pack(fill="x", side="bottom")
        self.footer_var = tk.StringVar(value="checking yt-dlp...")
        ttk.Label(footer, textvariable=self.footer_var,
                  style="Muted.TLabel").pack(side="left")
        self.check_btn = ttk.Button(footer, text="Check for updates", style="Link.TButton",
                                    command=lambda: self.check_for_updates(announce=True))
        self.check_btn.pack(side="right")

    def _build_links(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left",
                                                                    padx=(0, self.theme.px("md")))
        ttk.Label(header, text="Paste a link, or several, one per line",
                  style="Section.TLabel").pack(side="left")
        self.watch_clipboard_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="Auto-paste copied links",
                        variable=self.watch_clipboard_var,
                        command=self.on_watch_toggled).pack(side="right")

        # A classic Tk Text is the only multi-line field there is, so its
        # border and its focus ring have to be drawn by hand. Before this the
        # primary input of the application had a fainter focus indicator than
        # the two small entries that hold a timestamp.
        # undo=True: this is the main input of the app, and the clipboard
        # watcher writes into it on its own, so Ctrl+Z has to mean something.
        # Three lines rather than four: the fourth was 18px of white that the
        # queue needed more, and a box holding one link is the normal case.
        self.urls_box = tk.Text(parent, height=3, wrap="char", relief="flat",
                                undo=True, autoseparators=True, maxundo=-1,
                                borderwidth=0, highlightthickness=2,
                                highlightbackground=self.theme.c("border"),
                                highlightcolor=self.theme.c("accent"),
                                background=self.theme.c("field"),
                                font=self.theme.body,
                                padx=self.theme.px("sm"), pady=self.theme.px("xs"))
        self.urls_box.pack(fill="x", pady=self.theme.pad("sm", "xs"))

    def _build_message(self, parent):
        """One line under the box for anything that used to open a dialog.

        A native message box runs its own modal loop, which stops Tk's timers,
        which stops pump() -- so telling somebody they forgot a link would
        freeze the download that was already running.
        """
        self.message_var = tk.StringVar(value="")
        self.message = ttk.Label(parent, textvariable=self.message_var,
                                 style="Error.TLabel", justify="left", wraplength=self.theme.scaled(680))

    def _build_presets(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        # A fixed column, shared with the output row below. Both rows are
        # radio groups of the same size and colour, and with one of them
        # indented behind a label and the other starting at the margin they
        # read as a single group of six buttons.
        ttk.Label(row, text="Preset:", width=LABEL_COL,
                  anchor="w").pack(side="left")

        # A real preset, not Custom. Custom pre-selected on a window nobody has
        # touched reads as "you configured something" when it means the
        # opposite, and it can only ever be true after a manual change.
        self.preset_var = tk.StringVar(value="High quality")
        for preset in PRESETS:
            button = ttk.Radiobutton(row, text=preset["name"], value=preset["name"],
                                     variable=self.preset_var, command=self.apply_preset)
            button.pack(side="left", padx=(self.theme.px("md"), 0))
            # Focus as well as hover: arrowing through the group used to show
            # nothing, so the only way to find out what a preset did was to
            # pick it and have it rewrite your settings.
            button.bind("<Enter>", lambda _e, chosen=preset: self.preview_preset(chosen))
            button.bind("<FocusIn>", lambda _e, chosen=preset: self.preview_preset(chosen))
            button.bind("<Leave>", lambda _e: self.describe_preset())
            button.bind("<FocusOut>", lambda _e: self.describe_preset())
        ttk.Radiobutton(row, text=CUSTOM_PRESET, value=CUSTOM_PRESET,
                        variable=self.preset_var,
                        command=self.describe_preset).pack(side="left",
                                                           padx=(self.theme.px("md"), 0))

    def _build_choices(self, parent):
        choices = ttk.Frame(parent)
        choices.pack(fill="x", pady=self.theme.pad("md", "xs"))

        # Named, and on the same column as "Preset:" above it: the two groups
        # decide different things, and the only thing saying so was a 12px gap.
        ttk.Label(choices, text="Output:", width=LABEL_COL,
                  anchor="w").pack(side="left")
        self.mode_var = tk.StringVar(value="video")
        ttk.Radiobutton(choices, text="Video (MP4)", value="video",
                        variable=self.mode_var).pack(side="left",
                                                     padx=(self.theme.px("md"), 0))
        ttk.Radiobutton(choices, text="Audio only", value="audio",
                        variable=self.mode_var).pack(side="left",
                                                     padx=(self.theme.px("lg"), 0))

        self.quality_label = ttk.Label(choices, text="Quality:")
        self.quality_label.pack(side="left", padx=(self.theme.px("xl"), 0))
        self.quality_var = tk.StringVar(value="1080p")
        self.quality_box = ttk.Combobox(choices, values=VIDEO_QUALITIES, width=10,
                                        state="readonly", textvariable=self.quality_var)
        self.quality_box.pack(side="left", padx=(self.theme.px("sm"), 0))

        self.format_label = ttk.Label(choices, text="Audio format:")
        self.format_label.pack(side="left", padx=(self.theme.px("xl"), 0))
        self.format_var = tk.StringVar(value="mp3")
        self.format_box = ttk.Combobox(choices, values=AUDIO_FORMATS, width=8,
                                       state="readonly", textvariable=self.format_var)
        self.format_box.pack(side="left", padx=(self.theme.px("sm"), 0))

        self.bitrate_label = ttk.Label(choices, text="Bitrate:")
        self.bitrate_label.pack(side="left", padx=(self.theme.px("lg"), 0))
        self.bitrate_var = tk.StringVar(value="192")
        self.bitrate_box = ttk.Combobox(choices, values=AUDIO_BITRATES, width=6,
                                        state="readonly", textvariable=self.bitrate_var)
        self.bitrate_box.pack(side="left", padx=(self.theme.px("sm"), 0))

        # The sentence saying what all of the above will actually produce. It
        # is the most useful line in the window and used to be styled as the
        # least important thing on screen.
        self.preset_note = ttk.Label(parent, text="", justify="left", wraplength=self.theme.scaled(680))
        self.preset_note.pack(anchor="w")
        self.gate_note = ttk.Label(parent, text="", style="Muted.TLabel",
                                   justify="left", wraplength=self.theme.scaled(680))
        self.gate_note.pack(anchor="w", pady=(self.theme.px("xs"), 0))

    def _build_disclosure(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        self.disclosure_var = tk.BooleanVar(value=False)
        self.disclosure = ttk.Checkbutton(row, style="Toolbutton",
                                          text="▸  More options",
                                          variable=self.disclosure_var,
                                          command=self.toggle_extras)
        self.disclosure.pack(side="left")
        # Folded is not the same as hidden: if anything in there is switched on
        # it has to say so from out here.
        self.disclosure_note = ttk.Label(row, text="", style="Muted.TLabel")
        self.disclosure_note.pack(side="left", padx=(self.theme.px("sm"), 0))

    def _build_extras(self, parent):
        # Indented under the disclosure that opens it. Opened, these rows used
        # to start on the same left edge as everything else, so nothing said
        # they belonged to "More options" rather than to the window.
        self.extras = ttk.Frame(parent, padding=(self.theme.px("lg"), 0, 0, 0))
        self.extras.pack(fill="x")

        row = ttk.Frame(self.extras)
        row.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        self.playlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Download the whole playlist",
                        variable=self.playlist_var).pack(side="left")

        self.subs_label = ttk.Label(row, text="Subtitles:")
        self.subs_label.pack(side="left", padx=(self.theme.px("xl"), 0))
        self.subs_var = tk.StringVar(value="none")
        self.subs_box = ttk.Combobox(row, values=SUBTITLE_LANGS, width=6,
                                     state="readonly", textvariable=self.subs_var)
        self.subs_box.pack(side="left", padx=self.theme.pad("sm", "sm"))
        self.subs_var.trace_add("write", self.sync_fields)

        self.embed_subs_var = tk.BooleanVar(value=True)
        self.embed_subs_check = ttk.Checkbutton(row, text="inside the video, not a .srt file",
                                                variable=self.embed_subs_var)
        self.embed_subs_check.pack(side="left")

        self.sponsor_var = tk.BooleanVar(value=False)
        self.sponsor_check = ttk.Checkbutton(row, text="Cut sponsor segments (SponsorBlock)",
                                             variable=self.sponsor_var)
        self.sponsor_check.pack(side="left", padx=(self.theme.px("xl"), 0))

        clip = ttk.Frame(self.extras)
        clip.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        # One column for the two labelled rows of the panel: they held 61px
        # and 88px of text, so their fields started 27px apart on rows sitting
        # one above the other.
        ttk.Label(clip, text="Only from:", width=FIELD_COL,
                  anchor="w").pack(side="left")
        self.section_start_var = tk.StringVar(value="")
        self.section_start = ttk.Entry(clip, textvariable=self.section_start_var, width=9)
        self.section_start.pack(side="left", padx=self.theme.pad("sm", "sm"))
        ttk.Label(clip, text="to:").pack(side="left")
        self.section_end_var = tk.StringVar(value="")
        self.section_end = ttk.Entry(clip, textvariable=self.section_end_var, width=9)
        self.section_end.pack(side="left", padx=self.theme.pad("sm", "sm"))
        self.trim_button = ttk.Button(clip, text="Pick on a timeline...",
                                      command=self.open_clip_editor)
        self.trim_button.pack(side="left", padx=(0, self.theme.px("sm")))
        ttk.Label(clip, text="leave empty for the whole thing; 1:30 or 90 or 1:02:03",
                  style="Muted.TLabel").pack(side="left")

        login = ttk.Frame(self.extras)
        login.pack(fill="x", pady=self.theme.pad("xs", "xs"))
        ttk.Label(login, text="Sign-in cookies:", width=FIELD_COL,
                  anchor="w").pack(side="left")
        self.browser_var = tk.StringVar(value="none")
        # safari and whale on a Windows machine are noise, and picking one is
        # a failure that only announces itself after the download starts.
        browsers = [name for name in BROWSERS
                    if not (name == "safari" and sys.platform != "darwin")]
        ttk.Combobox(login, values=browsers, width=10, state="readonly",
                     textvariable=self.browser_var).pack(side="left",
                                                         padx=self.theme.pad("sm", "sm"))
        self.cookie_file_var = tk.StringVar(value="")
        ttk.Button(login, text="cookies.txt...",
                   command=self.pick_cookie_file).pack(side="left")
        # What was chosen, and a way back out of it. Before this the only trace
        # of a selected file was one line in the log.
        self.cookie_name = ttk.Label(login, text="", style="Muted.TLabel")
        self.cookie_name.pack(side="left", padx=(self.theme.px("sm"), 0))
        self.cookie_clear = ttk.Button(login, text="clear", style="Link.TButton",
                                       command=self.clear_cookie_file)
        self.browser_note = ttk.Label(self.extras, text="", style="Muted.TLabel",
                                      justify="left", wraplength=self.theme.scaled(680))
        self.browser_note.pack(anchor="w")
        self.browser_var.trace_add("write", self.sync_login)

    def _build_destination(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        # Same column as "Preset:" and "Output:", and the same 12px to the
        # control, so the three labels of the form share one left edge.
        ttk.Label(row, text="Save to:", width=LABEL_COL,
                  anchor="w").pack(side="left")
        self.dest_var = tk.StringVar(value=self.fallback_dir)
        ttk.Entry(row, textvariable=self.dest_var).pack(side="left", fill="x",
                                                        expand=True,
                                                        padx=self.theme.pad("md", "sm"))
        ttk.Button(row, text="Browse...", command=self.choose_folder).pack(side="left")
        ttk.Button(row, text="Open folder",
                   command=self.open_folder).pack(side="left", padx=(self.theme.px("sm"), 0))

    def _build_buttons(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=self.theme.pad("md", "xs"))
        self.download_btn = ttk.Button(row, text="Download", style="Primary.TButton",
                                       command=self.start)
        self.download_btn.pack(side="left")
        # "Preview" begged the question of what it previews, and the note
        # beside it answers the rest of the question the name cannot. It no
        # longer points at the trim window for the pattern: the caption there
        # speaks only when it has something to report.
        self.preview_btn = ttk.Button(row, text="Check details", command=self.preview)
        self.preview_btn.pack(side="left", padx=(self.theme.px("sm"), 0))
        ttk.Label(row, text="shows size and quality without downloading",
                  style="Muted.TLabel").pack(side="left",
                                             padx=(self.theme.px("sm"), 0))
        ttk.Label(row, text="Ctrl+Enter", style="Muted.TLabel").pack(side="right")

    def _build_progress(self, parent):
        self.bar = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=self.theme.pad("xs", "xs"))

        self.status_row = ttk.Frame(parent)
        row = self.status_row
        row.pack(fill="x")
        self.status_var = tk.StringVar(value=IDLE_STATUS)
        ttk.Label(row, textvariable=self.status_var).pack(side="left")
        # Speed, ETA and the byte count, which used to be the first characters
        # the queue's fixed-width column threw away.
        self.active_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.active_var,
                  style="Muted.TLabel").pack(side="right")

        self.preview_var = tk.StringVar(value="")
        # Its own line: sharing one StringVar with the queue meant any queue
        # event wiped "Reading ..." within a tenth of a second. It is packed
        # after the status row rather than on its own, because pack with no
        # anchor appends, and appending put "Reading ..." under the log box.
        self.preview_line = ttk.Label(parent, textvariable=self.preview_var,
                                      style="Muted.TLabel")

    def _build_queue(self, parent):
        frame = ttk.Frame(parent)
        parent.add(frame, weight=4)

        # Creation order and pack order do different jobs here, and they want
        # opposite things. Tab follows creation, so the list has to exist
        # before the buttons that act on it; layout follows packing, and the
        # toolbar has to claim the bottom strip first or a pane dragged small
        # loses the buttons instead of losing rows. So: build the list, then
        # build the toolbar, then pack them in the other order.
        scrollbar = ttk.Scrollbar(frame)
        # Five rows, not six: the rows are 24px tall now instead of 20, and
        # the pane asks for the same height it always did.
        self.tree = ttk.Treeview(frame, columns=("settings", "status", "progress"),
                                 show="tree headings", height=5,
                                 selectmode="extended", yscrollcommand=scrollbar.set)
        # Headings left-aligned, like the cells under them.
        self.tree.heading("#0", text="Link", anchor="w")
        self.tree.heading("settings", text="Settings", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.heading("progress", text="Progress", anchor="w")
        self.tree.column("#0", width=240, minwidth=140, stretch=True)
        self.tree.column("settings", width=165, minwidth=90, stretch=False, anchor="w")
        # Measured, like the progress column, and for the same reason: at a
        # flat 90px the longest word in the table -- "Downloading", 88px of
        # mono -- lost its last letter to the cell padding, and it is the
        # word the row shows for most of its life. The gap is the padding the
        # Treeview puts either side of the text.
        self.tree.column("status", stretch=False, anchor="w",
                         width=max(self.theme.measure(self.theme.mono, spec["label"])
                                   for spec in STATUS_TABLE.values()) + self.theme.px("md"),
                         minwidth=70)
        # Measured rather than guessed, so the bar, the percentage and the
        # speed beside them fit. The longer sentences a postprocessor produces
        # are shown without a bar in front of them, so they are not the case
        # this has to size for.
        widest = self.theme.measure(
            self.theme.mono, progress_bar(100) + " 100.0%  12.34 MB/s  ETA 3600s")
        self.tree.column("progress", width=widest, minwidth=180, stretch=True, anchor="w")
        self.tree.configure(style="Queue.Treeview")
        for name, spec in STATUS_TABLE.items():
            self.tree.tag_configure(name, foreground=self.theme.c(spec["tone"]))
        self.tree.tag_configure(EMPTY_ROW, foreground=self.theme.c("muted"))
        self._build_queue_tools(frame)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.tree.yview)

        self.tree.bind("<Double-1>", self.open_item_folder)
        self.tree.bind("<Delete>", lambda _event: self.clear_selected())
        # The key labelled Delete on a Mac keyboard sends BackSpace.
        self.tree.bind("<BackSpace>", lambda _event: self.clear_selected())
        self.tree.bind("<Return>", self.open_item_folder)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.refresh_queue_tools())

        self.row_menu = tk.Menu(self.tree, tearoff=0, postcommand=self.sync_row_menu)
        self.row_menu.add_command(label="Retry", command=self.retry_selected)
        self.row_menu.add_command(
            label="Pause", command=lambda: [self.pause_item(i) for i in self.selected_items()])
        self.row_menu.add_command(
            label="Resume", command=lambda: [self.resume_item(i) for i in self.selected_items()])
        self.row_menu.add_separator()
        self.row_menu.add_command(label="Copy link", command=self.copy_selected_link)
        self.row_menu.add_command(label="Why did it fail?", command=self.show_row_detail)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="Cancel", command=self.cancel_selected)
        self.row_menu.add_command(label="Remove", command=self.clear_selected)
        self.row_menu.add_command(label="Open folder", command=self.open_item_folder)
        self.tree.bind("<Button-3>", self.show_row_menu)
        self.tree.bind("<Button-2>", self.show_row_menu)  # right button on a Mac trackpad
        # Everything above was mouse-only, and the per-item pause, resume and
        # retry live nowhere else.
        self.tree.bind("<Shift-F10>", self.show_row_menu)
        self.tree.bind("<Key-App>", self.show_row_menu)

    def _build_queue_tools(self, parent):
        row = ttk.Frame(parent, padding=(0, self.theme.px("xs"), 0, 0))
        row.pack(side="bottom", fill="x")
        self.pause_btn = ttk.Button(row, text="Pause", state="disabled",
                                    command=self.on_pause_pressed)
        self.pause_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row, text="Cancel selected", state="disabled",
                                     command=self.cancel_selected)
        self.cancel_btn.pack(side="left", padx=(self.theme.px("sm"), 0))
        self.cancel_all_btn = ttk.Button(row, text="Cancel all", state="disabled",
                                         command=self.cancel_all)
        self.cancel_all_btn.pack(side="left", padx=(self.theme.px("sm"), 0))
        self.clear_btn = ttk.Button(row, text="Clear finished", state="disabled",
                                    command=self.clear_finished)
        self.clear_btn.pack(side="left", padx=(self.theme.px("sm"), 0))

        # A limit that applies to the download in flight belongs with the
        # queue, not in the status area it used to crowd out.
        self.rate_text = tk.StringVar(value="no limit")
        ttk.Label(row, textvariable=self.rate_text, style="Muted.TLabel",
                  width=10, anchor="e").pack(side="right")
        self.rate_var = tk.DoubleVar(value=0.0)
        ttk.Scale(row, from_=0.0, to=10.0, variable=self.rate_var, length=140,
                  command=self.on_rate_changed).pack(side="right",
                                                     padx=self.theme.pad("sm", "sm"))
        ttk.Label(row, text="Speed limit:", style="Muted.TLabel").pack(side="right")

    def _build_log(self, parent):
        frame = ttk.Frame(parent)
        # One against the queue's four: this is a debug console, and it used to
        # be both the largest and the highest-contrast object in the window.
        parent.add(frame, weight=1)

        header = ttk.Frame(frame)
        header.pack(side="top", fill="x", pady=self.theme.pad("xs", "xs"))
        ttk.Label(header, text="Log", style="Section.TLabel").pack(side="left")
        # In their own frame, packed left to right. Packed straight onto the
        # header with side="right" the first one created lands furthest right,
        # so the reading order and the Tab order came out reversed.
        actions = ttk.Frame(header)
        actions.pack(side="right")
        ttk.Button(actions, text="Copy", style="Link.TButton",
                   command=self.copy_log).pack(side="left",
                                               padx=(0, self.theme.px("sm")))
        ttk.Button(actions, text="Clear", style="Link.TButton",
                   command=self.clear_log).pack(side="left")

        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        # Two lines is what this pane insists on, not what it usually gets:
        # a ttk panedwindow hands every pane its requested height first and
        # only then shares what is left by weight, so four lines here were
        # four lines the queue could not have on a screen with no room to
        # spare. With space about, the 4:1 weight still gives it five or six.
        self.log_box = tk.Text(frame, height=2, wrap="word", state="disabled",
                               relief="flat", borderwidth=0,
                               background=self.theme.c("sunken"),
                               foreground=self.theme.c("on_sunken"),
                               insertbackground=self.theme.c("on_sunken"),
                               font=self.theme.mono,
                               yscrollcommand=scrollbar.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.log_box.yview)
        # This box is always dark -- sunken and on_sunken are the same in both
        # palettes -- so the tags have to come from the dark ramp too. Taking
        # them from the active palette meant that on any platform resolving to
        # the light one the error red landed at 2.5:1 on near-black, and the
        # first message a machine without ffmpeg ever shows was the least
        # readable thing in the window.
        for role in ("error", "warning", "success", "muted"):
            self.log_box.tag_configure(role, foreground=DARK_PALETTE[role])

    def _bind_keys(self):
        """What the window answers to. All of this was missing.

        A Text swallows Tab -- it inserts one and stops the traversal chain --
        so the app's primary input was a trap two stops from a fresh window.
        """
        self.urls_box.bind("<Tab>", self._focus_next)
        self.urls_box.bind("<Shift-Tab>", self._focus_prev)
        self.urls_box.bind("<Control-Return>", self._start_from_key)
        self.window.bind("<Control-Return>", self._start_from_key)
        self.window.bind("<Return>", self._return_pressed)
        self.window.bind("<Escape>", self._escape_pressed)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

    def _focus_next(self, _event):
        self.urls_box.tk_focusNext().focus_set()
        return "break"

    def _focus_prev(self, _event):
        self.urls_box.tk_focusPrev().focus_set()
        return "break"

    def _start_from_key(self, _event=None):
        self.start()
        return "break"

    def _return_pressed(self, event):
        # Enter inside any text field belongs to that field; anywhere else it
        # is the button the window would have marked as default if ttk had
        # one. Entries were missing from that list, so confirming "1:30" in a
        # clip box started the whole download.
        typing = (tk.Text, ttk.Entry, tk.Entry)
        if isinstance(event.widget, typing) or event.widget is self.tree:
            return None
        self.start()
        return "break"

    def _escape_pressed(self, _event):
        if self.banner.winfo_ismapped():
            self.hide_banner()
        return None

    # ---------------------------------------------------------------- widgets
    def log(self, text, role=None):
        if role is None:
            lowered = text.lstrip().lower()
            if lowered.startswith("[error]"):
                role = "error"
            elif lowered.startswith("[warning]"):
                role = "warning"
            elif lowered.startswith(("[update]", "[clipboard]", "[cookies]", "[cancelled]",
                                     "[convert]")):
                role = "muted"
        self.log_box.configure(state="normal")
        start = self.log_box.index("end-1c")
        self.log_box.insert("end", text + "\n")
        if role:
            self.log_box.tag_add(role, start, "end-1c")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def copy_log(self):
        """The log holds every full error, and it could not be selected."""
        text = self.log_box.get("1.0", "end-1c")
        if not text.strip():
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(text)
        self.say("The log is on the clipboard.", "muted")

    def say(self, text, tone="error"):
        """The inline replacement for a modal dialog."""
        self.message_var.set(text)
        self.message.configure(style={"error": "Error.TLabel",
                                      "muted": "Muted.TLabel",
                                      "warning": "Warning.TLabel"}.get(tone, "Error.TLabel"))
        if not self.message.winfo_ismapped():
            # after=, or pack appends it to the end of the frame: "Paste a
            # link first." was landing under the Download button, 320px below
            # the box it is about and below the field that had just been given
            # the focus. The label is not a tab stop, so only pixels move.
            self.message.pack(after=self.urls_box, anchor="w", fill="x",
                              pady=(0, self.theme.px("xs")))

    def hush(self):
        if self.message.winfo_ismapped():
            self.message.pack_forget()

    # ---------------------------------------------------------------- presets
    def current_settings(self):
        """The choices as they stand, as a plain dict."""
        return {
            "mode": self.mode_var.get(),
            "quality": self.quality_var.get(),
            "audio_format": self.format_var.get(),
            "bitrate": self.bitrate_var.get(),
        }

    def apply_preset(self):
        preset = find_preset(self.preset_var.get())
        if preset is None:
            return
        self.applying_preset = True  # keeps the change from reading as manual
        try:
            for name, value in preset["settings"].items():
                self.setting_vars[name].set(value)
        finally:
            self.applying_preset = False
        self.describe_preset()

    def on_manual_change(self, *_args):
        """Touching a dropdown means the choices are the user's own now."""
        if self.applying_preset:
            return
        self.preset_var.set(CUSTOM_PRESET)
        self.describe_preset()

    def describe_preset(self, *_args):
        preset = find_preset(self.preset_var.get())
        settings = dict(self.current_settings(), **preset["settings"]) if preset \
            else self.current_settings()
        self.preset_note.configure(
            text=describe_settings(settings, bool(self.ffmpeg_dir)))

    def preview_preset(self, preset):
        self.preset_note.configure(
            text=describe_settings(dict(self.current_settings(), **preset["settings"]),
                                   bool(self.ffmpeg_dir)))

    def sync_fields(self, *_args):
        """Which controls apply right now, and a sentence saying why.

        A greyed control with no reason beside it reads as a bug. Worse, ttk
        drops disabled widgets out of the tab order, so the route through the
        form silently changes shape when the mode does.
        """
        audio = self.mode_var.get() == "audio"
        has_ffmpeg = bool(self.ffmpeg_dir)
        self.quality_box.configure(state="disabled" if audio else "readonly")
        self.format_box.configure(state="readonly" if audio else "disabled")
        self.bitrate_box.configure(state="readonly" if audio else "disabled")
        for label, off in ((self.quality_label, audio),
                           (self.format_label, not audio),
                           (self.bitrate_label, not audio)):
            label.configure(style="Off.TLabel" if off else "TLabel")

        subs_off = audio or not has_ffmpeg
        self.subs_box.configure(state="disabled" if subs_off else "readonly")
        self.subs_label.configure(style="Off.TLabel" if subs_off else "TLabel")
        self.embed_subs_check.configure(
            state="disabled" if subs_off or self.subs_var.get() == "none" else "normal")
        self.sponsor_check.configure(state="disabled" if not has_ffmpeg else "normal")

        reasons = []
        if audio:
            reasons.append("Resolution and subtitles apply to video downloads.")
        else:
            reasons.append("Audio format and bitrate apply to audio-only downloads.")
        if not has_ffmpeg:
            reasons.append("Subtitles and SponsorBlock need ffmpeg, which is not installed.")
        self.gate_note.configure(text=" ".join(reasons))
        self.describe_preset()

    def sync_login(self, *_args):
        """Say the Chromium thing before it fails, not afterwards."""
        browser = self.browser_var.get()
        if browser != "none" and self.cookie_file_var.get():
            # yt-dlp would be handed both; the file is the one that was chosen
            # most recently by hand, so the browser gives way.
            self.clear_cookie_file()
        if sys.platform == "win32" and browser in ("chrome", "edge", "brave", "chromium",
                                                   "opera", "vivaldi", "whale"):
            self.browser_note.configure(
                text="Close %s completely first, including any icon in the system tray -- "
                     "it keeps its cookies locked while it runs. Firefox does not, and a "
                     "cookies.txt always works." % browser)
        else:
            self.browser_note.configure(text="")
        self.refresh_disclosure()

    def toggle_extras(self):
        self.extras_open = bool(self.disclosure_var.get())
        if self.extras_open:
            self.extras.pack(fill="x", after=self.disclosure.master)
            self.disclosure.configure(text="▾  More options")
        else:
            self.extras.pack_forget()
            self.disclosure.configure(text="▸  More options")
        self.refresh_disclosure()
        if self.extras_open and self.ready:
            # The panel needs room that the queue would otherwise have to give
            # up. Take it from the screen while there is any left.
            self.window.update_idletasks()
            wanted = self.window.winfo_reqheight()
            if self.window.winfo_height() < wanted:
                room = min(wanted, self.window.winfo_screenheight() - 80)
                self.window.geometry("%dx%d" % (self.window.winfo_width(), room))

    def refresh_disclosure(self, *_args):
        """Count what is switched on in there, so folded never means hidden."""
        on = []
        if self.playlist_var.get():
            on.append("playlist")
        if self.subs_var.get() != "none":
            on.append("subtitles")
        if self.sponsor_var.get():
            on.append("SponsorBlock")
        if self.section_start_var.get().strip() or self.section_end_var.get().strip():
            on.append("clip")
        if self.browser_var.get() != "none" or self.cookie_file_var.get():
            on.append("sign-in")
        self.disclosure_note.configure(
            text="" if self.extras_open or not on else "on: " + ", ".join(on))

    def pick_cookie_file(self):
        chosen = filedialog.askopenfilename(
            title="Select a cookies.txt file",
            filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")],
        )
        if not chosen:
            return  # Cancel must not wipe what was already chosen.
        self.cookie_file_var.set(chosen)
        self.log("[cookies] file selected: " + os.path.basename(chosen))
        self.browser_var.set("none")
        self.show_cookie_file()

    def clear_cookie_file(self):
        self.cookie_file_var.set("")
        self.show_cookie_file()

    def show_cookie_file(self):
        path = self.cookie_file_var.get()
        self.cookie_name.configure(text=os.path.basename(path) if path else "")
        # winfo_manager, not winfo_ismapped: the same trap show_banner had.
        # This button lives inside the collapsible "More options" panel, so
        # while that panel is folded the whole chain is unmapped and
        # winfo_ismapped() answers False even though the button is already
        # packed -- which skipped pack_forget() and left "clear" showing next
        # to an empty cookie file name the next time the panel was opened.
        if path and not self.cookie_clear.winfo_manager():
            self.cookie_clear.pack(side="left")
        elif not path and self.cookie_clear.winfo_manager():
            self.cookie_clear.pack_forget()

    def choose_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or self.fallback_dir)
        if chosen:
            self.dest_var.set(chosen)

    def open_folder(self):
        target = self.dest_var.get()
        if not os.path.isdir(target):
            self.say("That folder does not exist yet. It is created when the first "
                     "download starts.", "muted")
            return
        problem = open_in_file_manager(target)
        if problem:
            self.log("[warning] " + problem)

    # ----------------------------------------------------------------- yt-dlp
    def check_for_updates(self, announce=False):
        """Ask PyPI what the current yt-dlp is, off the Tk thread."""
        if self.checking:
            return
        self.checking = True
        self.check_btn.configure(state="disabled")
        threading.Thread(target=self._check_worker, args=(announce,), daemon=True).start()

    def _check_worker(self, announce):
        result = {"announce": announce, "latest": None, "problem": None,
                  "detail": None, "installed": None}
        try:
            result["installed"] = installed_ytdlp_version()
            if result["installed"] is None:
                result["problem"] = "not installed"
            else:
                result["latest"] = latest_ytdlp_version()
        except Exception as exc:  # noqa: BLE001
            # Being offline is normal and not worth a dialog. The event has
            # to go out anyway, or the Check button stays disabled for good.
            result["problem"] = "update check failed"
            result["detail"] = str(exc)
        self.events.put(("engine", result))

    def on_engine(self, result):
        self.checking = False
        self.check_btn.configure(state="normal")
        self.installed_version = result["installed"]
        self.latest_version = result["latest"]
        self.refresh_footer(result["problem"])
        self.hint = self.stale_hint()  # what a failing item will be told to say
        if result["detail"]:
            self.log("[update] could not reach PyPI: " + result["detail"])

        self.engine_ok = result["installed"] is not None
        self.gate_engine()
        if result["installed"] is None:
            self.show_banner("yt-dlp is not installed, so nothing can be downloaded yet.",
                             "Install now")
            self.refresh_status()
            return
        if result["latest"] and parse_version(result["latest"]) > parse_version(result["installed"]):
            self.show_banner(
                "yt-dlp %s is out and you have %s. Downloads that started failing usually "
                "work again after this." % (result["latest"], result["installed"]),
                "Update now")
        elif result["announce"] and result["latest"]:
            self.log("[update] yt-dlp %s is the current release." % result["installed"])
        self.refresh_status()

    def refresh_footer(self, problem=None):
        parts = ["yt-dlp " + self.installed_version if self.installed_version
                 else "yt-dlp missing"]
        parts.append("ffmpeg found" if self.ffmpeg_dir else "no ffmpeg")
        if problem and problem != "not installed":
            parts.append(problem)
        self.footer_var.set("   |   ".join(parts))

    def show_banner(self, text, button_label):
        self.banner_var.set(text)
        # winfo_manager, not winfo_ismapped: a widget inside a frame that is
        # not on screen yet reports itself unmapped even though pack is
        # already managing it. The banner is built hidden, so the "no ffmpeg"
        # message -- which asks for no button at all -- came up offering
        # "Update now", which updates yt-dlp and does nothing about ffmpeg.
        if button_label:
            self.update_btn.configure(text=button_label, state="normal")
            if not self.update_btn.winfo_manager():
                self.update_btn.pack(side="right", padx=self.theme.pad("sm", "sm"))
        elif self.update_btn.winfo_manager():
            self.update_btn.pack_forget()
        self.dismiss_btn.configure(text="Later")
        self.banner.pack(fill="x", side="top", before=self.top)

    def hide_banner(self):
        self.banner.pack_forget()

    def start_update(self):
        self.update_btn.configure(state="disabled")
        self.banner_var.set("Updating yt-dlp...")
        self.log("[update] python -m pip install -U yt-dlp")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            ok, message = update_ytdlp(lambda line: self.events.put(("log", line)))
        except Exception as exc:  # noqa: BLE001
            # Or the banner sits on "Updating yt-dlp..." with a dead button.
            ok, message = False, "The update could not run: %r" % (exc,)
        self.events.put(("updated", (ok, message)))

    def on_updated(self, ok, message):
        self.log("[update] " + message)
        self.banner_var.set(message)
        if ok:
            self.update_btn.pack_forget()
            self.dismiss_btn.configure(text="OK")
            self.installed_version = installed_ytdlp_version()
            self.latest_version = self.installed_version
            self.engine_ok = self.installed_version is not None
            self.gate_engine()
            self.refresh_footer()
            self.hint = self.stale_hint()
            self.refresh_status()
        else:
            self.update_btn.configure(state="normal")

    def gate_engine(self):
        """A button that cannot do its job should not be pressable.

        The banner used to say nothing could be downloaded while Download sat
        there fully enabled, which is an invitation to press it and get a
        pip command in a dialog box.
        """
        state = "disabled" if self.engine_ok is False else "normal"
        self.download_btn.configure(state=state)
        self.preview_btn.configure(state=state)
        if self.engine_ok is False:
            self.say("yt-dlp is missing, so there is nothing to download with. "
                     "Use 'Install now' at the top of the window.")

    def stale_hint(self):
        """What to tell a worker to say when an extractor looks out of date."""
        if (self.latest_version and self.installed_version
                and parse_version(self.latest_version) > parse_version(self.installed_version)):
            return ("yt-dlp %s is out and you are on %s. Click '%s' at the top of the window, "
                    "then use Retry on this row."
                    % (self.latest_version, self.installed_version, self.update_btn.cget("text")))
        return UPDATE_HINT

    # ----------------------------------------------------------------- events
    def pump(self):
        """Drain the worker events, and keep draining whatever happens."""
        try:
            self._drain_events()
        except Exception as exc:  # noqa: BLE001
            self.log("[error] the window could not finish an update: %r" % (exc,))
        finally:
            self.window.after(120, self.pump)

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "engine":
                    self.on_engine(payload)
                elif kind == "updated":
                    self.on_updated(*payload)
                elif kind == "item":
                    item = payload
                    self.refresh_row(item)
                    self.refresh_status()
                elif kind == "item-progress":
                    item, pct, stats = payload
                    if item.status not in ("Starting", "Downloading",
                                           "Retrying", "Converting"):
                        continue
                    item.last_event = time.monotonic()
                    item.progress = pct
                    item.detail = format_progress(stats, verbose=False)
                    if item.status != "Downloading" and pct < 100:
                        item.status = "Downloading"
                        self.refresh_status()
                    if stats:
                        self.active_var.set(format_progress(stats, verbose=True))
                    self.refresh_row(item)
                    self.refresh_bar()
                elif kind == "item-title":
                    item, title = payload
                    if title and title != item.label:
                        item.label = title
                        self.refresh_row(item)
                elif kind == "item-stage":
                    item, stage, before_download = payload
                    if item.finished:
                        continue
                    item.last_event = time.monotonic()
                    # A subtitle convertor runs before the download, not after
                    # it. Calling that "Processing" left every row claiming to
                    # be finished downloading for the whole download.
                    if not before_download and item.status in ("Starting", "Downloading"):
                        item.status = "Converting"
                        self.refresh_status()
                    item.detail = stage_label(stage)
                    self.refresh_row(item)
                    self.refresh_bar()
                elif kind == "preview":
                    self.on_preview(*payload)
                elif kind == "queue-idle":
                    self.on_queue_idle()
        except queue.Empty:
            pass
        self.watch_for_stalls()

    def watch_for_stalls(self):
        """yt-dlp retries quietly, five times, with warnings switched off.

        From outside, a row that is retrying and a row that has hung look
        exactly alike: both stop moving and say nothing.
        """
        now = time.monotonic()
        changed = False
        for item in list(self.rows.values()):
            if item.status == "Downloading" and now - item.last_event > STALL_SECONDS:
                item.status = "Retrying"
                item.detail = "no data for %ds - retrying" % int(now - item.last_event)
                self.refresh_row(item)
                changed = True
        if changed:
            self.refresh_status()
            self.refresh_bar()

    def on_queue_idle(self):
        self.refresh_status()
        self.active_var.set("")
        failed = [i for i in self.rows.values() if i.status in ("Failed", "Partial")]
        done = [i for i in self.rows.values() if i.status == "Done"]
        if done or failed:
            # Nothing reached an unfocused window before this: no sound, no
            # taskbar change, nothing. For an app whose whole premise is
            # "start it and go and do something else" that was the biggest
            # gap in it.
            try:
                self.window.bell()
            except Exception:  # noqa: BLE001
                pass
        self.window.title(self.base_title)

    # ------------------------------------------------------------------ queue
    def pending_urls(self):
        return [line.strip() for line in self.urls_box.get("1.0", "end").splitlines()
                if line.strip()]

    def clip_range(self):
        """The two time fields as seconds, or None when they make no sense."""
        for widget, variable, name in ((self.section_start, self.section_start_var, "from"),
                                       (self.section_end, self.section_end_var, "to")):
            try:
                parse_timestamp(variable.get())
            except ValueError as exc:
                self.open_extras()
                widget.focus_set()
                self.say("'%s' in the '%s' box is not a time. Write 1:30, or 90, "
                         "or 1:02:03." % (exc, name))
                return None
        start = parse_timestamp(self.section_start_var.get())
        end = parse_timestamp(self.section_end_var.get())
        if start is not None and end is not None and end <= start:
            self.open_extras()
            self.section_end.focus_set()
            self.say("The end of the clip has to come after its start.")
            return None
        return (start, end)

    def _remember_download(self, item, converter):
        """Note what landed, so the next attempt at the same link can say so.

        The height comes off the file rather than out of the format that was
        requested. Those two disagree exactly when it matters most: if yt-dlp
        skipped the download because the name was taken, the request says
        1080p and the file on disk is still the old 720p.
        """
        try:
            info = dict(converter.last_info or {})
            measured = probe_height(converter.final_file, self.ffmpeg_dir)
            if measured:
                info["height"] = measured
            index = LibraryIndex(item.settings["out_dir"])
            index.record(item.url, info, converter.final_file, item.settings)
        except Exception:  # noqa: BLE001
            pass  # a download that worked is never reported as a failure here

    def skip_what_we_have(self, urls, settings):
        """Drop the links whose file is already in the folder, once asked.

        One dialog for the whole batch, not one per link: pasting a playlist
        you mostly own would otherwise be twenty prompts in a row.

        Returns (links to queue, replace) -- `replace` is the subset of those
        links (as a set) the person agreed to overwrite. It is never wider
        than "the links that were actually found on disk": a fresh link
        pasted in the same batch as a duplicate must not be handed
        overwrites=True for a file it has no relationship to.

        Anything unexpected lets every link through. The point of this is to
        save a download, never to block one.
        """
        try:
            index = LibraryIndex(settings["out_dir"])
            known = [(url, index.find(url)) for url in urls]
            known = [(url, entry) for url, entry in known if entry]
        except Exception:  # noqa: BLE001
            return urls, set()
        if not known:
            return urls, set()

        if len(known) == 1:
            headline, question = describe_existing(known[0][1], settings)
        else:
            listed = "\n".join(
                "  %s%s" % (entry.get("title") or url,
                            "  (%sp)" % entry["height"] if entry.get("height") else "")
                for url, entry in known[:8])
            more = "\n  ...and %d more" % (len(known) - 8) if len(known) > 8 else ""
            headline = "%d of these are already in that folder:\n%s%s" % (
                len(known), listed, more)
            question = "Download them again anyway?"

        known_urls = {url for url, _ in known}
        if messagebox.askyesno(APP_NAME, headline + "\n\n" + question,
                               parent=self.window):
            # Saying yes has to produce the better file. yt-dlp skips a
            # download whose target name is already taken, so without this the
            # answer was honoured by leaving the old copy exactly where it was.
            # Only the links that were actually matched get overwrites=True --
            # a fresh link pasted in the same batch is not one of them.
            return urls, known_urls
        keep = [url for url in urls if url not in known_urls]
        self.log("Skipped %d already in %s" % (len(known), settings["out_dir"]))
        if not keep:
            # "muted", not the default: skipping something you already have
            # is an answer, not a failure, and say() paints anything it does
            # not recognise in the error style.
            self.say("Nothing to add -- you already have %s."
                     % ("that one" if len(known) == 1 else "all of those"),
                     tone="muted")
        return keep, set()

    def open_clip_editor(self):
        """Choose the two ends of a clip by eye instead of by memory."""
        if not self.ffmpeg_dir:
            self.say("Picking a clip on a timeline needs ffmpeg, and it is not "
                     "installed. The two time boxes still work.")
            return

        urls = self.pending_urls()
        if not urls:
            selected = self.selected_items()
            if selected:
                urls = [selected[0].url]
        if not urls:
            self.urls_box.focus_set()
            self.say("Paste a link first -- the timeline has to have something "
                     "to show you.")
            return
        if len(urls) > 1:
            self.say("Trimming works on one video at a time; this uses the first "
                     "link in the box.")

        try:
            start = parse_timestamp(self.section_start_var.get())
            end = parse_timestamp(self.section_end_var.get())
        except ValueError:
            start, end = None, None
        if start is not None and end is not None and end <= start:
            # clip_range() refuses exactly this when a download starts; the
            # picker has to refuse it too, or a stale "From 0:10 / To 0:05"
            # opens the timeline with the selection already inverted -- which
            # draws as the whole strip dimmed and nothing visibly selected.
            start, end = None, None

        editor = ClipEditor(self, urls[0], start, end)
        self.window.wait_window(editor.window)
        self.apply_clip_result(editor.result)

    def apply_clip_result(self, result):
        """Put what the timeline chose into the two time boxes.

        Its own method so the write-back can be exercised without a nested
        event loop -- and so there is one copy of the rule below rather than
        one in the app and one in whatever drives it.
        """
        if result is None:
            return
        chosen_start, chosen_end = result
        # One decimal: whole seconds can collapse two different marks onto the
        # same text, and clip_range() then reads that as an end before its
        # start and drops the clip without a word.
        self.section_start_var.set("" if chosen_start is None
                                   else clock_label(chosen_start, 1))
        self.section_end_var.set("" if chosen_end is None
                                 else clock_label(chosen_end, 1))
        self.hush()

    def open_extras(self):
        if not self.extras_open:
            self.disclosure_var.set(True)
            self.toggle_extras()

    def download_settings(self):
        """Everything a queue item needs, as it stands right now, or None if
        the form does not add up."""
        clip = self.clip_range()
        if clip is None:
            return None
        out_dir = self.dest_var.get().strip() or self.fallback_dir
        self.dest_var.set(out_dir)
        return dict(self.current_settings(),
                    out_dir=out_dir,
                    playlist=self.playlist_var.get(),
                    subtitles=self.subs_var.get(),
                    embed_subs=self.embed_subs_var.get(),
                    sponsorblock=self.sponsor_var.get(),
                    section_start=clip[0],
                    section_end=clip[1],
                    browser=self.browser_var.get(),
                    cookie_file=self.cookie_file_var.get() or None)

    def start(self):
        """Put the links in the box on the queue and make sure it is moving."""
        self.hush()
        urls = self.pending_urls()
        if not urls:
            self.say("Paste at least one link first.")
            self.urls_box.focus_set()
            return
        good = [url for url in urls if url.lower().startswith(("http://", "https://"))]
        bad = [url for url in urls if url not in good]
        if not good:
            self.say("That does not look like a link. A link starts with http:// "
                     "or https://.")
            self.urls_box.focus_set()
            return
        settings = self.download_settings()
        if settings is None:
            return
        self.enqueue(good, settings)
        self.urls_box.delete("1.0", "end")
        if bad:
            # Keep what could not be used rather than swallowing it.
            self.urls_box.insert("1.0", "\n".join(bad))
            self.say("%d line%s did not look like a link and %s left in the box."
                     % (len(bad), "" if len(bad) == 1 else "s",
                        "was" if len(bad) == 1 else "were"), "warning")

    def enqueue(self, urls, settings, label=None, ask=True):
        replace_urls = set()
        if ask:
            urls, replace_urls = self.skip_what_we_have(urls, settings)
        if not urls:
            return
        self.hide_empty_row()
        for url in urls:
            self.next_uid += 1
            # Per URL, not for the whole batch: accepting the upgrade for one
            # duplicate must not hand overwrites=True to every other link
            # pasted alongside it.
            item_settings = dict(settings, replace=True) if url in replace_urls else dict(settings)
            item = QueueItem(self.next_uid, url, item_settings, label=label)
            with self.lock:
                self.items.append(item)
            self.rows[item.uid] = item
            self.tree.insert("", "end", iid=str(item.uid), text=item.label,
                             values=(item.summary, "Queued", "waiting"),
                             tags=("Queued",))
        last = str(self.next_uid)
        self.tree.see(last)
        # Without a focus item the Treeview ignores the arrow keys entirely,
        # which took pause, resume, cancel and remove with it.
        if not self.tree.focus():
            self.tree.focus(last)
            self.tree.selection_set(last)
        self.refresh_status()
        self.ensure_worker()

    def show_empty_row(self):
        """Just the fact, in the first column.

        The instruction that used to sit beside this lived in the Progress
        column, hundreds of pixels away under a heading it had nothing to do
        with. It does not belong in a table at all: a cell is as wide as its
        column, and widening the column to fit a sentence made the whole
        window open wider. refresh_status() says it instead, on a full-width
        line that has room for it.
        """
        if self.rows or self.tree.exists(EMPTY_ROW):
            return
        self.tree.insert("", "end", iid=EMPTY_ROW, text="Nothing queued yet.",
                         values=("", "", ""), tags=(EMPTY_ROW,))

    def hide_empty_row(self):
        if self.tree.exists(EMPTY_ROW):
            self.tree.delete(EMPTY_ROW)

    # ---------------------------------------------------------------- preview
    def preview(self):
        """Ask the site what the first link holds, before committing to it."""
        self.hush()
        urls = self.pending_urls()
        if not urls:
            self.say("Paste a link first.")
            self.urls_box.focus_set()
            return
        settings = self.download_settings()
        if settings is None:
            return
        if len(urls) > 1:
            self.say("Details are read from the first link; the rest stay in the box.",
                     "muted")
        self.preview_btn.configure(state="disabled")
        self.preview_var.set("Reading %s ..." % urls[0])
        if not self.preview_line.winfo_ismapped():
            self.preview_line.pack(after=self.status_row, anchor="w")
        threading.Thread(target=self._preview_worker, args=(urls[0], settings),
                         daemon=True).start()

    def _preview_worker(self, url, settings):
        converter = Converter(on_log=lambda msg: self.events.put(("log", str(msg))),
                              browser=settings.get("browser"),
                              cookie_file=settings.get("cookie_file"),
                              update_hint=self.hint)
        try:
            data = converter.inspect(url, settings)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("preview", (None, converter._explain(exc), settings)))
            return
        data["thumbnail_file"] = fetch_thumbnail(data.get("thumbnail"), self.ffmpeg_dir)
        self.events.put(("preview", (data, None, settings)))

    def on_preview(self, data, problem, settings):
        self.preview_btn.configure(state="normal")
        self.preview_var.set("")
        self.preview_line.pack_forget()
        if problem:
            self.log("[error] " + problem)
            self.say(problem.splitlines()[0])
            if len(problem.splitlines()) > 1:
                self.log(problem)
            return
        self.show_preview(data, settings)

    def show_preview(self, data, settings):
        window = tk.Toplevel(self.window)
        window.title("Details - " + data["title"][:60])
        window.transient(self.window)
        window.bind("<Escape>", lambda _event: window.destroy())

        buttons = ttk.Frame(window, padding=(self.theme.px("md"), 0,
                                             self.theme.px("md"), self.theme.px("md")))
        # Packed to the bottom before the body claims the space, or the two
        # actions are the first things a small window clips.
        buttons.pack(side="bottom", fill="x")

        head = ttk.Frame(window, padding=self.theme.px("md"))
        head.pack(fill="x")

        picture = data.get("thumbnail_file")
        if picture:
            try:
                window._thumb = tk.PhotoImage(file=picture)
                ttk.Label(head, image=window._thumb).pack(side="left",
                                                          padx=(0, self.theme.px("md")),
                                                          anchor="n")
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    os.remove(picture)  # Tk has read it into memory by now
                except OSError:
                    pass

        text = ttk.Frame(head)
        text.pack(side="left", fill="x", expand=True)
        wrap = self.theme.scaled(700 if not picture else 420)
        ttk.Label(text, text=data["title"], style="Head.TLabel",
                  wraplength=wrap, justify="left").pack(anchor="w")

        facts = [part for part in (data["uploader"], format_duration(data["duration"]),
                                   format_date(data["upload_date"])) if part]
        if data["playlist"]:
            facts.append("in a playlist of %s" % data["playlist"]["count"])
        ttk.Label(text, text="   ".join(facts), style="Muted.TLabel").pack(anchor="w")

        chosen = data["chosen"]
        ttk.Label(text, text=describe_settings(settings, bool(self.ffmpeg_dir)),
                  wraplength=wrap, justify="left").pack(anchor="w",
                                                        pady=(self.theme.px("md"), 0))
        clipped = "" if not chosen["clip"] else ", clipped to %s" % chosen["clip"]
        ttk.Label(text, text="anydl would fetch %s (%s), about %s%s" % (
            chosen["format_id"], chosen["ext"], format_size(chosen["bytes"]), clipped),
            wraplength=wrap, justify="left").pack(anchor="w")
        for part in chosen["parts"]:
            ttk.Label(text, text="      %s  %s  %s  %s" % (
                part["id"], part["resolution"], part["vcodec"] or part["acodec"],
                format_size(part["bytes"])), style="Muted.TLabel").pack(anchor="w")
        if data["converted"] and data["converted"]["bytes"]:
            ttk.Label(text, text="      the converted file lands around %s"
                      % format_size(data["converted"]["bytes"]),
                      style="Muted.TLabel").pack(anchor="w")

        body = ttk.Frame(window, padding=(self.theme.px("md"), self.theme.px("sm"),
                                          self.theme.px("md"), self.theme.px("md")))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Everything the site offers:",
                  style="Section.TLabel").pack(anchor="w")
        scrollbar = ttk.Scrollbar(body)
        scrollbar.pack(side="right", fill="y")
        columns = ("ext", "resolution", "fps", "video", "audio", "size", "note")
        table = ttk.Treeview(body, columns=columns, show="tree headings",
                             yscrollcommand=scrollbar.set, style="Queue.Treeview")
        table.heading("#0", text="id")
        table.column("#0", width=90, stretch=False)
        for name, width in zip(columns, (50, 100, 45, 80, 80, 80, 150)):
            table.heading(name, text=name)
            table.column(name, width=width, stretch=(name == "note"))
        for fmt in data["formats"]:
            table.insert("", "end", text=fmt["id"], values=(
                fmt["ext"], fmt["resolution"], fmt["fps"], fmt["vcodec"], fmt["acodec"],
                format_size(fmt["bytes"]), fmt["note"]))
        table.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=table.yview)

        def add_and_close():
            # The title is already known here; queueing by URL alone left the
            # row showing a raw link until it started downloading.
            self.enqueue([data["url"]], settings, label=data["title"])
            self.drop_from_box(data["url"])
            window.destroy()

        add = ttk.Button(buttons, text="Add to queue", style="Primary.TButton",
                         command=add_and_close)
        add.pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")

        window.update_idletasks()
        window.minsize(640, 420)
        window.geometry("%dx%d" % (max(760, window.winfo_reqwidth()),
                                   min(self.window.winfo_screenheight() - 120,
                                       max(520, window.winfo_reqheight()))))
        add.focus_set()

    def drop_from_box(self, url):
        lines = [line for line in self.pending_urls() if line != url]
        self.urls_box.delete("1.0", "end")
        self.urls_box.insert("1.0", "\n".join(lines))

    # -------------------------------------------------------------- clipboard
    def on_watch_toggled(self):
        """Remember what is on the clipboard now, so turning the watch on does
        not sweep up something copied an hour ago."""
        self.last_clipboard = self.read_clipboard()
        self.log("[clipboard] auto-pasting copied links."
                 if self.watch_clipboard_var.get() else "[clipboard] stopped.")

    def read_clipboard(self):
        try:
            return (self.window.clipboard_get() or "").strip()
        except Exception:  # noqa: BLE001
            # Empty, holding an image, or momentarily owned by another app.
            return None

    def poll_clipboard(self):
        if self.watch_clipboard_var.get():
            text = self.read_clipboard()
            # A failed read is not an empty clipboard. Treating it as one reset
            # the memory and let the same link come back in a second time.
            if text is not None and text != self.last_clipboard:
                self.last_clipboard = text
                self.take_copied_link(text)
        self.window.after(700, self.poll_clipboard)

    def take_copied_link(self, text):
        if len(text.split()) != 1 or not text.lower().startswith(("http://", "https://")):
            return
        if text in self.pending_urls():
            return
        # Appended rather than rewritten: rebuilding the whole box threw away
        # the caret and any half-typed line.
        current = self.urls_box.get("1.0", "end-1c")
        if current and not current.endswith("\n"):
            self.urls_box.insert("end", "\n")
        self.urls_box.insert("end", text + "\n")
        self.urls_box.see("end")
        self.log("[clipboard] added " + text)
        self.say("Added a link from the clipboard.", "muted")

    def ensure_worker(self):
        with self.lock:
            if self.worker_running:
                return
            self.worker_running = True
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _next_item(self):
        """Claim the next waiting item, or release the worker if there is none.

        Both happen under the lock start() takes, so a link added while the
        worker is winding up cannot end up sitting there with nothing running.
        """
        with self.lock:
            for item in self.items:
                if item.status == "Queued" and not item.cancelled:
                    item.status = "Starting"
                    item.last_event = time.monotonic()
                    return item
            self.worker_running = False
            return None

    def _queue_worker(self):
        try:
            while True:
                item = self._next_item()
                if item is None:
                    self.events.put(("queue-idle", None))
                    return
                try:
                    self._run_item(item)
                except Exception as exc:  # noqa: BLE001
                    # Whatever went wrong belongs to that one link.
                    self._fail_item(item, "%s: %s" % (type(exc).__name__, exc))
        except BaseException as exc:  # noqa: BLE001
            # A worker that dies without clearing this leaves the queue looking
            # busy for ever, and every link added afterwards just sits there.
            with self.lock:
                self.worker_running = False
            self.events.put(("log", "[error] the queue stopped: %r" % (exc,)))
            self.events.put(("queue-idle", None))
            raise

    def _fail_item(self, item, message):
        with self.lock:
            item.status, item.detail, item.converter = "Failed", message, None
            item.note = message
        self.events.put(("log", "[error] " + message))
        self.events.put(("item", item))

    def _run_item(self, item):
        converter = Converter(
            on_log=lambda msg: self.events.put(("log", str(msg))),
            on_progress=lambda pct, stats, this=item: self.events.put(
                ("item-progress", (this, pct, stats))),
            on_title=lambda title, this=item: self.events.put(("item-title", (this, title))),
            on_stage=lambda stage, before, this=item: self.events.put(
                ("item-stage", (this, stage, before))),
            browser=item.settings.get("browser"),
            cookie_file=item.settings.get("cookie_file"),
            update_hint=self.hint,
        )
        with self.lock:
            # A cancel that arrived while this was being set up would have
            # had nothing to set the flag on.
            converter.cancelled = item.cancelled or item.pause_requested
            converter.set_rate_limit(self.rate_limit)
            item.converter = converter

        self.events.put(("item", item))
        self.events.put(("log", "\n>> " + item.url))
        converter.announce_cookies()
        outcome, message = converter.run(item.url, item.settings)

        if outcome == "ok" and not item.cancelled:
            # Outside the lock on purpose: this writes a file, and the queue
            # should not wait on a disk to hand out the next item.
            self._remember_download(item, converter)

        with self.lock:
            item.converter = None
            item.note = message
            if item.cancelled:
                item.status = "Cancelled"
                item.detail = ("Cancelled at %.0f%% - the partial file is kept, so queueing "
                               "this link again carries on from here" % item.progress
                               if item.progress > 0 else "Cancelled before it started")
            elif item.pause_requested:
                item.pause_requested = False
                item.status = "Paused"
                item.detail = "Paused at %.0f%% - Resume carries on from here" % item.progress
            elif outcome == "ok":
                item.status, item.progress, item.detail = "Done", 100.0, message
            elif outcome == "partial":
                item.status, item.progress, item.detail = "Partial", 100.0, message
            else:
                item.status, item.detail = "Failed", message
        if item.status == "Failed":
            self.events.put(("log", "[error] " + message))
        elif item.status == "Partial":
            self.events.put(("log", "[warning] " + message))
        elif item.status == "Done":
            self.events.put(("log", "[done] %s -> %s" % (item.label,
                                                         item.settings["out_dir"])))
        elif item.status == "Cancelled" and item.progress > 0:
            # yt-dlp keeps what it had as a .part file, on purpose: queueing
            # the same link again picks up where this left off.
            self.events.put(("log", "[cancelled] %s - the partial file is still in the "
                                    "folder, and downloading it again resumes from there."
                             % item.label))
        self.events.put(("item", item))

    def on_rate_changed(self, *_args):
        """The cap applies to what is downloading now, not only to the next one."""
        megabytes = round(self.rate_var.get() * 2) / 2.0
        self.rate_limit = int(megabytes * 1048576) or None
        self.rate_text.set("%.1f MB/s" % megabytes if self.rate_limit else "no limit")
        with self.lock:
            live = [item.converter for item in self.items if item.converter]
        for converter in live:
            converter.set_rate_limit(self.rate_limit)

    # ------------------------------------------------------------ pause
    def pause_item(self, item):
        """Stop the download but keep the item, and the bytes already on disk.

        yt-dlp leaves a .part file behind and picks it up when the same link
        runs again, so resuming is a matter of putting the item back in line.
        """
        with self.lock:
            if item.status not in ("Downloading", "Retrying", "Starting"):
                return
            item.pause_requested = True
            item.status = "Pausing"
            # Or the row keeps the speed and the ETA it had a moment ago and
            # looks like it is still going at full tilt.
            item.detail = "Stopping - the bytes already on disk are kept"
            if item.converter:
                item.converter.cancelled = True
        self.refresh_row(item)
        self.refresh_status()

    def resume_item(self, item):
        with self.lock:
            if item.status != "Paused":
                return
            item.pause_requested = False
            item.status, item.detail = "Queued", "waiting"
        self.refresh_row(item)
        self.refresh_status()
        self.ensure_worker()

    def on_pause_pressed(self):
        running = [item for item in self.rows.values()
                   if item.status in ("Downloading", "Retrying", "Starting")]
        if running:
            for item in running:
                self.pause_item(item)
            return
        for item in list(self.rows.values()):
            if item.status == "Paused":
                self.resume_item(item)

    def refresh_pause_button(self):
        """Four labels, not two.

        Pressing Pause moves the row to Pausing, which used to match neither
        branch: the button greyed out still saying Pause, then silently turned
        into Resume whenever the worker got round to unwinding.
        """
        statuses = [item.status for item in self.rows.values()]
        moving = [s for s in statuses if s in ("Downloading", "Retrying", "Starting")]
        if "Pausing" in statuses:
            self.pause_btn.configure(text="Pausing...", state="disabled")
        elif moving:
            self.pause_btn.configure(
                text="Pause all" if len(moving) > 1 else "Pause", state="normal")
        elif "Paused" in statuses:
            paused = statuses.count("Paused")
            self.pause_btn.configure(
                text="Resume all" if paused > 1 else "Resume", state="normal")
        else:
            self.pause_btn.configure(text="Pause", state="disabled")

    def refresh_queue_tools(self):
        """A button that would do nothing should say so before it is pressed."""
        chosen = self.selected_items()
        alive = [item for item in self.rows.values() if not item.finished]
        self.cancel_btn.configure(
            state="normal" if any(not i.finished for i in chosen) else "disabled")
        self.cancel_all_btn.configure(state="normal" if alive else "disabled")
        self.clear_btn.configure(
            state="normal" if any(i.finished for i in self.rows.values()) else "disabled")

    def selected_items(self):
        chosen = []
        for uid in self.tree.selection():
            if uid.isdigit() and int(uid) in self.rows:
                chosen.append(self.rows[int(uid)])
        return chosen

    def sync_row_menu(self):
        """Grey what does not apply, rather than returning silently."""
        chosen = self.selected_items()
        def state(condition):
            return "normal" if condition else "disabled"
        self.row_menu.entryconfigure(
            "Retry", state=state(any(i.status in ("Failed", "Partial", "Cancelled")
                                     for i in chosen)))
        self.row_menu.entryconfigure(
            "Pause", state=state(any(i.status in ("Downloading", "Retrying", "Starting")
                                     for i in chosen)))
        self.row_menu.entryconfigure(
            "Resume", state=state(any(i.status == "Paused" for i in chosen)))
        self.row_menu.entryconfigure("Copy link", state=state(len(chosen) == 1))
        self.row_menu.entryconfigure(
            "Why did it fail?", state=state(any(i.note for i in chosen)))
        self.row_menu.entryconfigure("Cancel", state=state(any(not i.finished for i in chosen)))
        self.row_menu.entryconfigure("Remove", state=state(any(not i.busy for i in chosen)))
        self.row_menu.entryconfigure("Open folder", state=state(len(chosen) == 1))

    def show_row_menu(self, event=None):
        if event is not None and getattr(event, "y", None) is not None and event.num in (2, 3):
            row = self.tree.identify_row(event.y)
            if row and row not in self.tree.selection():
                self.tree.selection_set(row)
            where = (event.x_root, event.y_root)
        else:
            # Reached from the keyboard, so there is no pointer to ask.
            chosen = self.tree.selection()
            if not chosen:
                return "break"
            box = self.tree.bbox(chosen[0])
            if not box:
                return "break"
            where = (self.tree.winfo_rootx() + box[0] + box[2] // 2,
                     self.tree.winfo_rooty() + box[1] + box[3])
        if not self.selected_items():
            return "break"
        try:
            self.row_menu.tk_popup(where[0], where[1])
        finally:
            self.row_menu.grab_release()
        return "break"

    def copy_selected_link(self):
        chosen = self.selected_items()
        if not chosen:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(chosen[0].url)
        self.say("Link copied.", "muted")

    def show_row_detail(self):
        """The whole reason, not the first line of it.

        refresh_row keeps one line, and the column clips that -- so the half of
        the message that says what to do about it never reached anybody.
        """
        chosen = [item for item in self.selected_items() if item.note]
        if not chosen:
            return
        item = chosen[0]
        window = tk.Toplevel(self.window)
        window.title("%s - %s" % (STATUS_TABLE[item.status]["label"], APP_NAME))
        window.transient(self.window)
        window.bind("<Escape>", lambda _event: window.destroy())
        frame = ttk.Frame(window, padding=self.theme.px("md"))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=item.label, style="Section.TLabel",
                  wraplength=self.theme.scaled(520), justify="left").pack(anchor="w")
        ttk.Label(frame, text=item.url, style="Muted.TLabel",
                  wraplength=self.theme.scaled(520), justify="left").pack(anchor="w",
                                                       pady=(0, self.theme.px("sm")))
        ttk.Label(frame, text=item.note, wraplength=self.theme.scaled(520),
                  justify="left").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(self.theme.px("md"), 0))
        if item.status in ("Failed", "Partial", "Cancelled"):
            ttk.Button(row, text="Retry", style="Primary.TButton",
                       command=lambda: [self.retry_item(item), window.destroy()]).pack(side="left")
        ttk.Button(row, text="Copy link",
                   command=lambda: [self.window.clipboard_clear(),
                                    self.window.clipboard_append(item.url)]).pack(
            side="left", padx=(self.theme.px("sm"), 0))
        close = ttk.Button(row, text="Close", command=window.destroy)
        close.pack(side="right")
        close.focus_set()

    def retry_item(self, item):
        """Put the same link back with the same settings.

        The app already told people to update yt-dlp and try the link again --
        and then gave them no way to, because start() had cleared the box and
        the URL only survived inside a Treeview cell.
        """
        # No duplicate check: Retry is the person saying "yes, again".
        self.enqueue([item.url], item.settings, label=item.label, ask=False)

    def retry_selected(self):
        for item in self.selected_items():
            if item.status in ("Failed", "Partial", "Cancelled"):
                self.retry_item(item)

    def cancel_item(self, item):
        if item.finished:
            return
        with self.lock:
            item.cancelled = True
            if item.status == "Queued":
                item.status, item.detail = "Cancelled", "Cancelled before it started"
            else:
                item.status = "Cancelling"
                item.detail = ("Stopping - the bytes already on disk are kept"
                               if item.progress > 0 else "Stopping...")
                if item.converter:
                    item.converter.cancelled = True
        self.refresh_row(item)
        self.refresh_status()

    def cancel_selected(self):
        for item in self.selected_items():
            self.cancel_item(item)

    def cancel_all(self):
        live = [item for item in self.rows.values() if item.busy]
        if live and not messagebox.askyesno(
                APP_NAME,
                "Stop %d download%s in progress?\n\nWhat has already been fetched stays "
                "on disk, so queueing the same link again carries on from there."
                % (len(live), "" if len(live) == 1 else "s")):
            return
        for item in list(self.rows.values()):
            self.cancel_item(item)

    def clear_finished(self):
        self._remove([item for item in self.rows.values() if item.finished])

    def clear_selected(self):
        # Anything that is not moving can go, including a paused or waiting one.
        chosen = self.selected_items()
        doomed = [item for item in chosen if not item.busy]
        if chosen and not doomed:
            self.say("That row is still running. Cancel it first.", "muted")
            return
        self._remove(doomed)

    def _remove(self, doomed):
        if not doomed:
            return
        with self.lock:
            for item in doomed:
                self.items.remove(item)
        following = None
        for item in doomed:
            if self.tree.exists(str(item.uid)):
                following = self.tree.next(str(item.uid)) or self.tree.prev(str(item.uid))
            self.rows.pop(item.uid, None)
            self.tree.delete(str(item.uid))
        # Keep a focus item, or the arrow keys go dead again.
        if following and self.tree.exists(following):
            self.tree.focus(following)
            self.tree.selection_set(following)
        self.show_empty_row()
        self.refresh_status()

    def open_item_folder(self, _event=None):
        for item in self.selected_items():
            target = item.settings["out_dir"]
            if not os.path.isdir(target):
                self.say("%s does not exist yet." % target, "muted")
                return "break"
            problem = open_in_file_manager(target)
            if problem:
                self.log("[warning] " + problem)
            return "break"
        return "break"

    def on_close(self):
        live = [item for item in self.rows.values() if item.busy]
        if live and not messagebox.askyesno(
                APP_NAME,
                "%d download%s still running.\n\nClosing now stops %s. What has already "
                "been fetched stays on disk, so starting the same link again carries on "
                "from there.\n\nClose anyway?"
                % (len(live), " is" if len(live) == 1 else "s are",
                   "it" if len(live) == 1 else "them")):
            return
        self.window.destroy()

    # ------------------------------------------------------------- queue view
    def refresh_row(self, item):
        row = str(item.uid)
        if not self.tree.exists(row):
            return
        detail = item.detail.splitlines()[0] if item.detail else ""
        if item.status in ("Downloading", "Retrying"):
            detail = "%s %5.1f%%  %s" % (progress_bar(item.progress), item.progress, detail)
        elif item.status in ("Converting", "Pausing", "Cancelling"):
            # No bar at all here: a full one beside "Processing" read as
            # finished, and that is the longest wait in the whole app.
            detail = detail or STATUS_TABLE[item.status]["label"]
        elif item.status == "Paused":
            detail = "%s  %s" % (progress_bar(item.progress), detail)
        elif item.status == "Done":
            detail = "%s  %s" % (progress_bar(100), detail or "done")
        self.tree.item(row, text=item.label,
                       values=(item.summary, STATUS_TABLE[item.status]["label"], detail),
                       tags=(item.status,))

    def refresh_status(self):
        self.refresh_pause_button()
        self.refresh_queue_tools()
        self.refresh_bar()
        counts = {}
        for item in self.rows.values():
            counts[item.status] = counts.get(item.status, 0) + 1
        if not counts:
            self.status_var.set(IDLE_STATUS)
            self.active_var.set("")
            self.window.title(self.base_title)
            return
        self.status_var.set("   ".join(
            "%d %s" % (counts[name], STATUS_TABLE[name]["label"].lower())
            for name in STATUS_ORDER if counts.get(name)))
        # The title bar is the one completion channel that reaches an
        # unfocused window for free, and it shows up in Alt-Tab too.
        busy = [item for item in self.rows.values() if item.busy]
        if busy:
            done = sum(1 for item in self.rows.values() if item.finished)
            self.window.title("%d%% - %d of %d - %s" % (
                self._queue_fraction() * 100, done + 1, len(self.rows), self.base_title))
        else:
            self.window.title(self.base_title)

    def _queue_fraction(self):
        """How far the whole queue has got, not how far one item has."""
        if not self.rows:
            return 0.0
        total = len(self.rows)
        done = sum(1 for item in self.rows.values() if item.finished)
        running = sum(item.progress / 100.0 for item in self.rows.values()
                      if item.busy and item.status in ("Downloading", "Retrying"))
        return min(1.0, (done + running) / total)

    def refresh_bar(self):
        """One bar for the queue, and honest about not knowing.

        It used to follow whichever item was running, so it swept 0 to 100 once
        per link; it stayed at 100 after one finished; it sat at 100 for the
        whole of a conversion; and it showed a failure as 0, which is
        indistinguishable from not having started.
        """
        vague = [item for item in self.rows.values()
                 if item.status in ("Starting", "Converting", "Retrying", "Pausing",
                                    "Cancelling")
                 or (item.status == "Downloading" and item.progress <= 0)]
        want = "indeterminate" if vague else "determinate"
        if want != self.bar_mode:
            self.bar_mode = want
            if want == "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(60)
            else:
                self.bar.stop()
                self.bar.configure(mode="determinate")
        if want == "determinate":
            self.bar["value"] = self._queue_fraction() * 100


def launch_gui():
    # No _load_tk() here. That belongs to anydl.py, which publishes `anydl.tk`
    # and friends for callers that prepared Tk by hand; this module imports
    # tkinter at the top and needs nothing published. Calling it from here was
    # a leftover of the split into anydl_core, and it raised NameError on the
    # first line of every GUI launch.
    claim_taskbar_identity()  # must happen before the first window exists
    AnydlApp().run()
