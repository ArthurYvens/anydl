"""The trim window: filmstrip, handles, transcript."""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .captions import fetch_transcript
from .environment import ffmpeg_tool
from .media import ClipSource
from .text import clock_label, format_duration, parse_timestamp


class ClipEditor:
    """Pick the two ends of a clip by looking at the video instead of guessing.

    The two time boxes on the main window are exact and completely blind: you
    type 4:12 because you remember something happens around there, download
    ninety seconds, and find out you were twenty seconds early. This shows the
    frame under each handle while you drag it, so the cut is chosen by eye.

    Everything it draws comes from a `ClipSource` -- a small local copy and a
    grid of stills. Dragging reads a still that is already on disk, which is
    why it keeps up with the mouse; letting go decodes the real frame at that
    exact second, which takes about a second and corrects the rounding.
    """

    # The floor and the ceiling for the filmstrip, not its height: once the
    # tiles are on disk the strip takes the height they actually have, so
    # nothing is cropped and no dark band is left under a widescreen frame.
    # The ceiling is for portrait video, whose 144x256 tiles would otherwise
    # hand the strip 45% of the window and leave the transcript a sliver.
    STRIP_HEIGHT = 60
    STRIP_MAX = 120
    STILL_MAX = 264         # how tall the preview may be before it is halved
    RULER_HEIGHT = 20
    TILE_SUBSAMPLE = 2      # 288-wide stills become 144-wide tiles
    GRAB_SLOP = 7           # pixels either side of a handle that still grab it
    HANDLE_W = 10           # drawn inside the selection, so it never half-clips
    MIN_SPAN = 0.5          # shorter than this is a slip of the mouse, not a clip
    LINE_PADDING = 0.35     # air around a caption, which is timed to the word
    # How long to wait for ffplay to report itself before starting the
    # picture anyway. Measured start-up here is ~870ms; this is only the
    # ceiling for a build that never prints.
    AUDIO_WAIT_MAX = 2.5
    # Preparing a clip costs about a second for every five seconds of video,
    # so past this the wait stops being a blink and the caption says so.
    LONG_SPAN = 60.0
    # Below this the still under the playhead is the moment under the
    # playhead, and saying which one is showing is noise. A tenth of a second
    # is under one frame of any video this window will be handed.
    STILL_SLACK = 0.1

    def __init__(self, app, url, start, end):
        self.app = app
        self.theme = app.theme
        self.url = url
        self.source = ClipSource(url, app.ffmpeg_dir)
        self.result = None

        self.duration = 0.0
        self.t_in = start
        self.t_out = end
        self.playhead = start or 0.0
        self.dragging = None
        # The strip, the ruler and the handle in display pixels. Left as bare
        # constants they stayed 60, 20 and 10 physical pixels at 150%, where
        # everything around them is half as big again.
        self.strip = self.theme.scaled(self.STRIP_HEIGHT)
        self.ruler = self.theme.scaled(self.RULER_HEIGHT)
        self.handle_w = self.theme.scaled(self.HANDLE_W)
        self.slop = self.theme.scaled(self.GRAB_SLOP)
        self.tiles = []
        self.preview_image = None
        self.settle_token = 0
        # Playback: the stills for the current span, the clock it runs on, and
        # the pending after() id so stopping is immediate.
        self.play_frames = []
        self.play_fps = 0
        self.play_started = None   # None until the sound is actually up
        self.play_start = 0.0
        self.play_clock_job = None
        self.play_span = 0.0
        self.play_sound = False
        self.play_job = None
        self.play_image = None
        self.play_ahead = None       # next frame, decoded during the slack
        self.play_ahead_index = -1
        self.play_shown_index = -1
        self.play_clock_job = None
        self.preparing = False
        # Which run of playback we are on. A prepare that is still in ffmpeg
        # when Stop is pressed comes back with a stale token and is dropped,
        # which is what makes Stop mean something during the wait.
        self.play_token = 0
        self.play_note_state = None
        self.transcript = []     # every line the captions gave us
        self.shown_lines = []    # the subset the search box is showing
        # Same contract as the main window: a worker never touches a widget,
        # not even through after(). after() registers a Tcl command, which is
        # not thread safe -- it raises outright with no mainloop running, and
        # merely gets away with it when there is one.
        self.events = queue.Queue()
        self.alive = True

        self.window = tk.Toplevel(app.window)
        self.window.title("Trim - " + url[:60])
        self.window.transient(app.window)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", lambda _event: self.cancel())

        self._build()
        self.window.update_idletasks()
        # The transcript needs room to be worth having, and the numbers are
        # scaled: at 150% every font in here is half as big again, and a fixed
        # 880x620 would have clipped the buttons off the bottom.
        self.window.minsize(self.theme.scaled(880), self.theme.scaled(620))
        # wait_window alone runs a nested loop but leaves the main window
        # clickable, so Download could be pressed mid-trim and queue the old
        # times, which apply_clip_result would then overwrite.
        self.window.grab_set()
        self.pump()  # started on the main thread, and reschedules itself there
        threading.Thread(target=self._prepare_worker, daemon=True).start()

    # ---------------------------------------------------------------- layout
    def _build(self):
        pad = self.theme.px("md")

        head = ttk.Frame(self.window, padding=(pad, pad, pad, 0))
        head.pack(fill="x")
        self.title_label = ttk.Label(head, text="Reading the video...",
                                     style="Head.TLabel", wraplength=self.theme.scaled(660),
                                     justify="left")
        self.title_label.pack(anchor="w")

        body = ttk.Frame(self.window, padding=(pad, self.theme.px("sm"), pad, 0))
        body.pack(fill="both", expand=True)

        self.stage = ttk.Frame(body)
        self.stage.pack(fill="both", expand=True)
        # The frame and everything that describes it in one column, the words
        # in the other. The two blocks used to sit side by side with the still
        # alone on the left, which left roughly 292x198 of empty window under
        # it -- and put the two time boxes as far from the frame they are
        # reading as the panel allowed.
        facts = ttk.Frame(self.stage)
        facts.pack(side="left", anchor="n")
        # Sized to hold a 288x162 still, which is what a 16:9 video comes back
        # as and what most videos are: the label is in characters until the
        # image arrives, so anything smaller made the two time boxes under it
        # jump down the moment the video loaded.
        self.picture = tk.Label(facts, background=self.theme.c("sunken"),
                                foreground=self.theme.c("on_sunken"),
                                width=44, height=11,
                                text="Fetching a small copy to scrub through...")
        self.picture.pack(anchor="w")
        # The caption of the picture, and only that: the moment on screen. It
        # used to read "playhead 0:00   (nearest still: 0:00)" -- a word for
        # what a timecode under a frame plainly is, and a second number that
        # is only news while the still is still the rounded one.
        self.frame_label = ttk.Label(facts, text="", style="Muted.TLabel")
        self.frame_label.pack(anchor="w", pady=(self.theme.px("xs"), 0))

        # A gap rather than a rule: the two boxes are a separate thing from
        # the picture, and "lg" says so without drawing a line to say it.
        marks = ttk.Frame(facts)
        marks.pack(anchor="w", pady=(self.theme.px("lg"), 0))
        # Typable, not readonly. The main window accepts "1:30" in its two
        # boxes; the window that exists to choose times refused to, so anyone
        # who already knew the timestamp had to close it, type there, and
        # reopen.
        #
        # The labels are right-aligned so their edge is the field's edge:
        # "From" and "To" left-aligned left a ragged two-character step
        # between the word and the box it names.
        ttk.Label(marks, text="From").grid(row=0, column=0, sticky="e")
        self.in_var = tk.StringVar(value="")
        self.in_entry = ttk.Entry(marks, textvariable=self.in_var, width=10)
        self.in_entry.grid(row=0, column=1, padx=self.theme.pad("sm", "sm"))
        # "Set to playhead" twice said the same four words twice, and the
        # playhead is the frame above: these name the result instead.
        ttk.Button(marks, text="Start here", style="Link.TButton",
                   command=self.set_in).grid(row=0, column=2, sticky="w")
        ttk.Label(marks, text="To").grid(row=1, column=0, sticky="e")
        self.out_var = tk.StringVar(value="")
        self.out_entry = ttk.Entry(marks, textvariable=self.out_var, width=10)
        self.out_entry.grid(row=1, column=1, padx=self.theme.pad("sm", "sm"))
        ttk.Button(marks, text="End here", style="Link.TButton",
                   command=self.set_out).grid(row=1, column=2, sticky="w")
        for entry, which in ((self.in_entry, "in"), (self.out_entry, "out")):
            entry.bind("<Return>", lambda _e, w=which: self.commit_time(w))
            entry.bind("<FocusOut>", lambda _e, w=which: self.commit_time(w))

        words = ttk.Frame(self.stage)
        # The only division between the two columns, so it has to be wide
        # enough to be read as one. "md" is the gap between rows inside a
        # block, which is not what a column break should look like.
        words.pack(side="left", fill="both", expand=True, padx=(self.theme.px("xl"), 0))
        finder = ttk.Frame(words)
        finder.pack(fill="x")
        # The panel is titled once, which retires both the "Find in what is
        # said:" sentence in front of the box and the "said" column heading
        # inside the table.
        ttk.Label(finder, text="What is said", style="Section.TLabel").pack(side="left")
        self.search_var = tk.StringVar(value="")
        self.search_entry = ttk.Entry(finder, textvariable=self.search_var, width=18)
        self.search_entry.pack(side="right")
        ttk.Label(finder, text="Search", style="Muted.TLabel").pack(
            side="right", padx=(0, self.theme.px("sm")))
        self.search_var.trace_add("write", self.filter_transcript)

        holder = ttk.Frame(words)
        holder.pack(fill="both", expand=True, pady=(self.theme.px("xs"), 0))
        # A caption line is longer than any column this panel can spare, and
        # the sentence that decides the cut was the one running off the edge.
        # Packed before the others so it keeps its strip at the bottom.
        xbar = ttk.Scrollbar(holder, orient="horizontal")
        xbar.pack(side="bottom", fill="x")
        bar = ttk.Scrollbar(holder)
        bar.pack(side="right", fill="y")
        # No heading row. "at" and "said" restated the title above the box for
        # a stamp and a sentence that need no naming, and dropping the row
        # gives the transcript back the height it took.
        self.transcript_view = ttk.Treeview(
            holder, columns=("text",), show="tree", height=6,
            yscrollcommand=bar.set, xscrollcommand=xbar.set,
            style="Transcript.Treeview")
        self.transcript_view.column("#0", width=self.theme.scaled(64), stretch=False)
        self.transcript_view.column("text", width=self.theme.scaled(360))
        self.transcript_view.pack(side="left", fill="both", expand=True)
        bar.configure(command=self.transcript_view.yview)
        xbar.configure(command=self.transcript_view.xview)
        self.transcript_view.bind("<<TreeviewSelect>>", self.on_transcript_pick)
        self.transcript_view.bind("<Double-1>", lambda _e: self.clip_selected_lines())

        # What the lines are, and the one thing to do with them, on the same
        # line under the table they both refer to. The button used to sit up
        # beside the search box, three widgets away from the selection it
        # acts on -- and ahead of the table in the Tab order.
        picked = ttk.Frame(words)
        picked.pack(fill="x", pady=(self.theme.px("xs"), 0))
        # Blank until something is actually happening. It opened saying
        # "looking for captions..." while the proxy was still downloading and
        # nothing had gone looking yet.
        self.transcript_note = ttk.Label(picked, text="", style="Muted.TLabel")
        self.transcript_note.pack(side="left")
        self.clip_line_button = ttk.Button(picked, text="Clip these lines",
                                           command=self.clip_selected_lines,
                                           state="disabled")
        self.clip_line_button.pack(side="right")

        timeline = ttk.Frame(self.window, padding=(pad, self.theme.px("md"), pad, 0))
        timeline.pack(fill="x")
        self.canvas = tk.Canvas(timeline, height=self.strip + self.ruler,
                                highlightthickness=1,
                                highlightbackground=self.theme.c("border"),
                                background=self.theme.c("sunken"))
        self.canvas.pack(fill="x")
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        # The transport belongs to the strip, not to the window. Play, what
        # playback is doing, how long the selection is and the way back to all
        # of it are one row under the filmstrip they all describe, which
        # leaves the row along the bottom holding the two decisions that
        # actually close this window.
        transport = ttk.Frame(timeline)
        transport.pack(fill="x", pady=(self.theme.px("sm"), 0))
        # Fixed width, because the label changes: "Play" and "Stop" both
        # measure 58px at width=8, where the natural widths are 76px and 58px
        # and everything to the right of the button moved 18px the moment
        # playback started.
        self.play_button = ttk.Button(transport, text="Play", width=8,
                                      command=self.play, state="disabled")
        self.play_button.pack(side="left")
        self.whole_button = ttk.Button(transport, text="Whole video",
                                       style="Link.TButton", command=self.reset,
                                       state="disabled")
        self.whole_button.pack(side="right")
        self.length_label = ttk.Label(transport, text="", style="Muted.TLabel")
        self.length_label.pack(side="right", padx=(0, self.theme.px("sm")))
        self.play_note = ttk.Label(transport, text="", style="Muted.TLabel")
        self.play_note.pack(side="left", padx=(self.theme.px("sm"), 0))

        # One line, under the thing it is about. This was a three-line
        # paragraph in the left column, the largest block of text in the
        # window, explaining a strip it could not point at. The wrap is
        # scaled, so it still breaks in the right place at 150%.
        self.hint_label = ttk.Label(
            timeline, text="Drag the handles, or click the strip to move the "
                           "playhead. Arrows nudge, I and O set the ends, "
                           "space plays.",
            style="Muted.TLabel", justify="left",
            wraplength=self.theme.scaled(820))
        self.hint_label.pack(anchor="w", pady=(self.theme.px("xs"), 0))

        buttons = ttk.Frame(self.window, padding=pad)
        buttons.pack(fill="x")
        # Built in the order they are read, packed in the order they are
        # drawn. The two are independent -- creation sets the Tab ring and
        # pack sets the pixels -- and they used to disagree: Tab went from the
        # queue straight to "Use this clip" and then back left to Cancel.
        cancel = ttk.Button(buttons, text="Cancel", command=self.cancel)
        self.use_button = ttk.Button(buttons, text="Use this clip", style="Primary.TButton",
                                     command=self.accept, state="disabled")
        self.use_button.pack(side="right")
        cancel.pack(side="right", padx=(0, self.theme.px("sm")))

        for key in ("<Left>", "<Right>", "<Shift-Left>", "<Shift-Right>",
                    "<i>", "<o>", "<I>", "<O>", "<space>"):
            self.window.bind(key, self.on_key)

    # --------------------------------------------------------------- loading
    def _prepare_worker(self):
        try:
            self.source.prepare(self._stage_from_worker)
            if not self.source.cancelled:
                self.events.put(("ready", None))
        except Exception as exc:  # noqa: BLE001
            if not self.source.cancelled:
                self.events.put(("failed", str(exc)))

    def _stage_from_worker(self, text, fraction):
        if fraction is not None:
            text = "%s  %d%%" % (text, int(fraction * 100))
        self.events.put(("stage", text))

    def pump(self):
        """Drain what the workers posted. Main thread only."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "stage":
                    self.picture.configure(text=payload)
                elif kind == "ready":
                    self.on_ready()
                elif kind == "failed":
                    self.on_failed(payload)
                elif kind == "exact":
                    self._show_exact(*payload)
                elif kind == "transcript":
                    self.on_transcript(payload)
                elif kind == "audiostarted":
                    self._start_play_clock(payload)
                elif kind == "playready":
                    self.on_play_ready(*payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return  # the window went away mid-drain
        if self.alive:
            self.window.after(80, self.pump)

    def on_ready(self):
        self.duration = self.source.duration
        if self.t_out is None or self.t_out > self.duration:
            self.t_out = self.duration
        if self.t_in is None:
            self.t_in = 0.0
        self.playhead = self.t_in
        self.title_label.configure(text=self.source.title)
        # The window title is one of the few things a screen reader announces,
        # so spending it on a raw URL wastes the cheapest accessible channel
        # this window has.
        self.window.title("Trim - " + self.source.title[:60])
        self.picture.configure(text="", width=0, height=0)
        self.play_button.configure(state="normal")
        self.use_button.configure(state="normal")
        # Whole video resets the two ends, and there are no ends to reset
        # until the length is known: enabled from the start it was a control
        # that answered a click by doing nothing.
        self.whole_button.configure(state="normal")
        self.set_play_note(self.play_hint())
        self._load_tiles()
        self.redraw()
        self.show_frame(self.playhead)
        self.canvas.focus_set()
        # Captions are a separate trip to the site, so they load after the
        # timeline is already usable rather than holding it up.
        self.transcript_note.configure(text="looking for captions...")
        threading.Thread(target=self._transcript_worker, daemon=True).start()

    def _transcript_worker(self):
        try:
            lines = fetch_transcript(self.url)
        except Exception:  # noqa: BLE001
            lines = []
        if not self.source.cancelled:
            self.events.put(("transcript", lines))

    def on_transcript(self, lines):
        self.transcript = lines
        if not lines:
            self.transcript_note.configure(
                text="this one has no captions, so there is nothing to search")
            self.search_entry.configure(state="disabled")
            return
        self.transcript_note.configure(
            text="%d lines. Pick one or several, then clip them." % len(lines))
        self.filter_transcript()

    def filter_transcript(self, *_args):
        """Show the lines containing what was typed, all of them when empty."""
        needle = self.search_var.get().strip().lower()
        self.shown_lines = [line for line in self.transcript
                            if not needle or needle in line["text"].lower()]
        view = self.transcript_view
        view.delete(*view.get_children())
        for index, line in enumerate(self.shown_lines):
            view.insert("", "end", iid=str(index),
                        text=clock_label(line["start"]), values=(line["text"],))
        if needle:
            self.transcript_note.configure(
                text="%d of %d lines contain that" % (len(self.shown_lines),
                                                      len(self.transcript)))
        elif self.transcript:
            self.transcript_note.configure(
                text="%d lines. Pick one or several, then clip them."
                     % len(self.transcript))
        self.clip_line_button.configure(state="disabled")

    def _picked_lines(self):
        picked = []
        for iid in self.transcript_view.selection():
            try:
                picked.append(self.shown_lines[int(iid)])
            except (ValueError, IndexError):
                pass
        return sorted(picked, key=lambda line: line["start"])

    def on_transcript_pick(self, _event=None):
        """Selecting a line takes the playhead to the moment it is said."""
        picked = self._picked_lines()
        self.clip_line_button.configure(state="normal" if picked else "disabled")
        if not picked or self.duration <= 0:
            return
        self.playhead = min(max(picked[0]["start"], 0.0), self.duration)
        self.redraw()
        self.show_frame(self.playhead)
        self.settle(self.playhead)

    def clip_selected_lines(self):
        """Set the clip to exactly what those lines cover.

        A little air either side: captions are timed to when a word lands, so
        cutting on the exact stamp clips the first syllable.
        """
        picked = self._picked_lines()
        if not picked or self.duration <= 0:
            return
        self.t_in = max(0.0, picked[0]["start"] - self.LINE_PADDING)
        self.t_out = min(self.duration, picked[-1]["end"] + self.LINE_PADDING)
        if self.t_out - self.t_in < self.MIN_SPAN:
            self.t_out = min(self.duration, self.t_in + self.MIN_SPAN)
        self.playhead = self.t_in
        self.redraw()
        self.show_frame(self.playhead)
        self.settle(self.playhead)

    def on_failed(self, message):
        # Wrapped to the box it is written in. A fixed 380 was wider than the
        # 44-character placeholder the message lands in, so the text ran past
        # both edges of the dark panel and lost a word at each end: "...ming
        # on a timeline needs ffmpeg" was how it introduced itself.
        room = max(self.picture.winfo_width(), self.theme.scaled(280))
        self.picture.configure(text=message, wraplength=room - self.theme.px("lg"))
        self.title_label.configure(text="Cannot trim this one")
        # Nothing was built, so there are no handles to drag and no keys that
        # do anything. The line stays in place and stops giving instructions.
        self.hint_label.configure(text="")

    def _load_tiles(self):
        """One PhotoImage per still, shrunk to a strip tile. Tk only scales by
        whole numbers, so the still width and the tile width are chosen to be
        an exact multiple apart rather than resampled."""
        self.tiles = []
        for path in self.source.frames:
            try:
                full = tk.PhotoImage(file=path)
                self.tiles.append(full.subsample(self.TILE_SUBSAMPLE))
            except Exception:  # noqa: BLE001
                self.tiles.append(None)
        # The strip is as tall as a tile. A fixed 60 cropped the bottom off a
        # 4:3 frame and left a dark band under a 16:9 one, and it made the
        # main tool of this window shorter than the row of buttons below it.
        # The floor is not scaled and the ceiling is: a tile is pixels cut out
        # of a 288px-wide still, so it does not grow with the display, and a
        # scaled floor would put a 90px strip around an 81px tile and leave
        # the dark band back at 150%. The ceiling is a share of the window,
        # which does scale.
        real = [tile.height() for tile in self.tiles if tile]
        if real:
            self.strip = min(max(real[0], self.STRIP_HEIGHT),
                             self.theme.scaled(self.STRIP_MAX))
            self.canvas.configure(height=self.strip + self.ruler)

    # --------------------------------------------------------------- drawing
    def _width(self):
        return max(self.canvas.winfo_width(), 1)

    def x_for(self, seconds):
        if self.duration <= 0:
            return 0
        return int(self._width() * min(max(seconds, 0.0), self.duration) / self.duration)

    def time_for(self, x):
        if self.duration <= 0:
            return 0.0
        return min(max(float(x) / self._width() * self.duration, 0.0), self.duration)

    def redraw(self):
        canvas = self.canvas
        canvas.delete("all")
        if self.duration <= 0 or not self.tiles:
            return
        width = self._width()
        strip = self.strip

        tile_w = max((self.tiles[0].width() if self.tiles[0] else 96), 1)
        for left in range(0, width, tile_w):
            image = self._tile_at(self.time_for(left + tile_w / 2.0))
            if image:
                canvas.create_image(left, 0, image=image, anchor="nw")

        # Everything outside the clip is dimmed, so the selection reads as the
        # bright part rather than as two lines you have to find.
        for x0, x1 in ((0, self.x_for(self.t_in)), (self.x_for(self.t_out), width)):
            if x1 > x0:
                # gray25, not gray50: the heavier stipple reads as static over
                # the thumbnails, and what is outside the clip still has to be
                # recognisable enough to drag towards.
                canvas.create_rectangle(x0, 0, x1, strip, fill=self.theme.c("sunken"),
                                        stipple="gray25", outline="")
        canvas.create_rectangle(self.x_for(self.t_in), 0, self.x_for(self.t_out), strip,
                                outline=self.theme.c("accent"), width=2)

        canvas.create_rectangle(0, strip, width, strip + self.ruler,
                                fill=self.theme.c("sunken"), outline="")
        for seconds in self._ruler_marks():
            x = self.x_for(seconds)
            canvas.create_line(x, strip, x, strip + self.theme.px("xs"),
                               fill=self.theme.c("on_sunken"))
            # The theme font, not ("TkDefaultFont", 7): as a tuple Tk reads the
            # first item as a family, cannot find one by that name, and falls
            # back to Arial at a size that ignores both DPI and the OS setting.
            canvas.create_text(x + self.theme.px("xs"), strip + self.theme.px("xs"),
                               text=clock_label(seconds), anchor="nw",
                               fill=self.theme.c("on_sunken"), font=self.theme.small)

        # Every marker pairs something light with something dark, because one
        # flat colour vanishes over a frame of a similar shade -- which is what
        # the old dark amber playhead did over dark video. The handles are also
        # drawn inside the selection instead of centred on its edge: centred,
        # half of each one fell off the canvas whenever the whole video was
        # selected, which is the state the window opens in.
        span = self.handle_w
        for edge, is_start in ((self.x_for(self.t_in), True),
                               (self.x_for(self.t_out), False)):
            left = edge if is_start else edge - span
            left = min(max(left, 0), max(width - span, 0))
            canvas.create_rectangle(left, 0, left + span, strip,
                                    fill="#ffffff", outline=self.theme.c("sunken"))
            middle = left + span // 2
            thin = self.theme.scaled(2)
            for grip in (middle - thin, middle + thin):
                canvas.create_line(grip, strip * 0.32, grip, strip * 0.68,
                                   fill=self.theme.c("accent"), width=thin)

        self._draw_playhead()

        # One decimal, matching what Use this clip writes back, so the
        # readout is not quietly rounder than the cut.
        self.in_var.set(clock_label(self.t_in, 1))
        self.out_var.set(clock_label(self.t_out, 1))
        span = max(0.0, self.t_out - self.t_in)
        whole = span >= self.duration - 0.5 and self.t_in <= 0.5
        self.length_label.configure(
            text="the whole video, %s" % format_duration(self.duration) if whole
            else "clip is %s of %s" % (format_duration(span),
                                       format_duration(self.duration)))

    def _tile_at(self, seconds):
        if not self.tiles:
            return None
        index = int(round(seconds / self.source.step))
        return self.tiles[max(0, min(index, len(self.tiles) - 1))]

    def _ruler_marks(self):
        """Round numbers, about one label every 90 pixels."""
        if self.duration <= 0:
            return []
        wanted = max(2, self._width() // 90)
        for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if self.duration / step <= wanted:
                break
        return [mark * step for mark in range(1, int(self.duration // step) + 1)]

    # ------------------------------------------------------------ the frames
    def _fit_still(self, image):
        """Halve a portrait still until the preview stops dictating the window.

        The stills are 288px wide whatever the shape of the video, so a 9:16
        one comes back 512px tall. Shown at that size it pushed the two time
        boxes under it far enough down to make this window 949px tall, which
        does not fit on the screen the window is most likely to be opened on.
        Tk only resizes by whole numbers, so this halves and thirds.
        """
        factor = 1
        while image.height() // factor > self.theme.scaled(self.STILL_MAX):
            factor += 1
        return image.subsample(factor) if factor > 1 else image

    def show_frame(self, seconds, exact=False):
        path = self.source.frame_for(seconds)
        if path:
            try:
                self.preview_image = self._fit_still(tk.PhotoImage(file=path))
                self.picture.configure(image=self.preview_image, text="")
            except Exception:  # noqa: BLE001
                pass
        shown = self.source.frame_time(seconds)
        # One decimal, the same precision the two boxes and the cut itself
        # carry. The second number is the one caveat this caption owes -- the
        # frame on screen is the nearest still, not the moment named beside it
        # -- and it is spent only when the two actually differ. Landing on a
        # grid second is common, and "0:00.0   nearest still 0:00" is a
        # warning about nothing.
        off_grid = abs(shown - seconds) >= self.STILL_SLACK
        self.frame_label.configure(
            text="%s   nearest still %s" % (clock_label(seconds, 1),
                                            clock_label(shown))
            if off_grid and not exact else clock_label(seconds, 1))

    def settle(self, seconds):
        """After a drag, decode the real frame at that second.

        The grid rounds to a couple of seconds, which is close enough to aim
        with and not close enough to trust, so the moment the mouse is
        released the true frame replaces the approximation.
        """
        self.settle_token += 1
        token = self.settle_token

        def worker():
            path = self.source.exact_frame(seconds)
            if path and token == self.settle_token and not self.source.cancelled:
                self.events.put(("exact", (path, seconds, token)))

        threading.Thread(target=worker, daemon=True).start()

    def _show_exact(self, path, seconds, token):
        if token != self.settle_token:
            return
        try:
            self.preview_image = self._fit_still(tk.PhotoImage(file=path))
            self.picture.configure(image=self.preview_image, text="")
            self.frame_label.configure(text=clock_label(seconds, 1))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ----------------------------------------------------------- interaction
    def on_press(self, event):
        if self.duration <= 0:
            return
        # Matches where the handles are actually drawn. Measured from the
        # edge instead, the inner half of each one did not respond to a click.
        width = self._width()
        span = self.handle_w
        start_left = min(max(self.x_for(self.t_in), 0), max(width - span, 0))
        end_left = min(max(self.x_for(self.t_out) - span, 0), max(width - span, 0))
        near_in = start_left - self.slop <= event.x <= start_left + span + self.slop
        near_out = end_left - self.slop <= event.x <= end_left + span + self.slop
        if near_in and near_out:
            # Both ends on top of each other: take whichever side was clicked.
            near_out = event.x >= self.x_for(self.t_in)
            near_in = not near_out
        self.dragging = "in" if near_in else "out" if near_out else "head"
        self.canvas.focus_set()
        self.on_drag(event)

    def on_drag(self, event):
        if self.duration <= 0 or not self.dragging:
            return
        seconds = self.time_for(event.x)
        if self.dragging == "in":
            self.t_in = min(seconds, self.t_out - self.MIN_SPAN)
            self.playhead = self.t_in
        elif self.dragging == "out":
            self.t_out = max(seconds, self.t_in + self.MIN_SPAN)
            self.playhead = self.t_out
        else:
            self.playhead = seconds
        self.redraw()
        self.show_frame(self.playhead)

    def on_release(self, _event):
        if self.dragging and self.duration > 0:
            self.settle(self.playhead)
        self.dragging = None

    def on_key(self, event):
        if self.duration <= 0:
            return None
        # These are bound on the toplevel, so they fire while a text field has
        # focus too. An Entry inserts the character and does not stop the
        # event, so typing "o zoo" into the search box moved the out point to
        # the playhead and started playback -- quietly destroying the selection
        # the person had just made.
        focused = self.window.focus_get()
        if isinstance(focused, (ttk.Entry, tk.Entry, tk.Text, ttk.Combobox)):
            return None
        key = event.keysym.lower()
        if key == "space":
            self.play()
            return "break"
        if key == "i":
            self.set_in()
            return "break"
        if key == "o":
            self.set_out()
            return "break"
        step = 5.0 if event.state & 0x0001 else 1.0
        if key == "left":
            self.playhead = max(0.0, self.playhead - step)
        elif key == "right":
            self.playhead = min(self.duration, self.playhead + step)
        else:
            return None
        self.redraw()
        self.show_frame(self.playhead)
        self.settle(self.playhead)
        return "break"

    def set_in(self):
        if self.duration <= 0:
            return
        self.t_in = min(self.playhead, self.t_out - self.MIN_SPAN)
        self.redraw()

    def set_out(self):
        if self.duration <= 0:
            return
        self.t_out = max(self.playhead, self.t_in + self.MIN_SPAN)
        self.redraw()

    def commit_time(self, which):
        """Take a typed time, or quietly put back the one that was there.

        Bound to Return and to losing focus, so a half-finished value never
        becomes the clip. parse_timestamp is the same reader the main window
        uses, so "1:30", "90" and "1:02:03" all mean what they mean there, and
        anything it rejects reverts rather than raising.
        """
        if self.duration <= 0:
            return "break"
        variable = self.in_var if which == "in" else self.out_var
        try:
            seconds = parse_timestamp(variable.get())
        except ValueError:
            seconds = None
        if seconds is None:
            self.redraw()  # rewrites both boxes from the values actually held
            return "break"

        seconds = min(max(seconds, 0.0), self.duration)
        if which == "in":
            self.t_in = min(seconds, self.t_out - self.MIN_SPAN)
            self.playhead = self.t_in
        else:
            self.t_out = max(seconds, self.t_in + self.MIN_SPAN)
            self.playhead = self.t_out
        self.redraw()
        self.show_frame(self.playhead)
        self.settle(self.playhead)
        return "break"

    def reset(self):
        if self.duration <= 0:
            return
        self.t_in, self.t_out = 0.0, self.duration
        self.playhead = 0.0
        self.redraw()
        self.show_frame(0.0)

    def _draw_playhead(self):
        """The playhead, tagged so playback can move it on its own.

        Redrawing the whole canvas at twelve frames a second would rebuild
        every filmstrip tile on every tick; this is the one item that has to
        keep up.
        """
        if self.duration <= 0:
            return
        canvas = self.canvas
        canvas.delete("playhead")
        strip = self.strip
        thin, thick = self.theme.scaled(2), self.theme.scaled(4)
        width = self._width()
        head = min(max(self.x_for(self.playhead), thin), max(width - thin, thin))
        canvas.create_line(head, 0, head, strip, fill=self.theme.c("sunken"),
                           width=thick, tags="playhead")
        canvas.create_line(head, 0, head, strip, fill="#ffcc00",
                           width=thin, tags="playhead")

    # --------------------------------------------------------------- playing
    def play(self):
        """Play the selection inside this window, or stop what is running."""
        if self.duration <= 0:
            return
        if self.preparing or self.play_job is not None:
            # Stop has to mean stop during the wait as well. The button
            # already said "Stop" while ffmpeg was decoding, and did nothing
            # at all when pressed there -- on a long span, several seconds of
            # a control lying about what it does.
            self.stop_playback()
            return

        start, end = self.t_in, self.t_out
        self.play_token += 1
        self.preparing = True
        self.play_button.configure(text="Stop", state="normal")
        self.set_play_note(self._preparing_note(end - start))
        self._busy_cursor(True)
        threading.Thread(target=self._prepare_playback,
                         args=(start, end, self.play_token), daemon=True).start()

    def _preparing_note(self, span):
        """What the caption says while ffmpeg decodes the span.

        The number is the span itself, because that is what the wait is
        proportional to -- about a second for every five seconds of video.
        "preparing..." on its own said nothing about whether to expect a
        blink or half a minute.
        """
        if span > self.LONG_SPAN:
            return "preparing %s - this takes a moment" % clock_label(span)
        return "preparing %s..." % clock_label(max(span, 0.0))

    def _prepare_playback(self, start, end, token):
        try:
            frames, fps = self.source.preview_frames(start, end)
        except Exception:  # noqa: BLE001
            frames, fps = [], 0
        if not self.source.cancelled:
            self.events.put(("playready", (frames, fps, start, end, token)))

    def on_play_ready(self, frames, fps, start, end, token):
        # Stopped, or asked for a different span, while this one was decoding.
        # The frames go with the temporary folder; what matters here is that
        # the window does not start playing something already called off.
        if token != self.play_token:
            return
        self.preparing = False
        self._busy_cursor(False)
        if not frames or not self.alive:
            self.set_play_note("ffmpeg could not decode that span", bad=True)
            self.play_button.configure(text="Play")
            return

        self.play_frames = frames
        self.play_fps = fps or self.source.PLAY_FPS
        self.play_start = start
        self.play_span = max(0.0, end - start)
        # Sound is optional: a proxy without an audio track, or a machine
        # without ffplay, still gets the picture.
        self.play_sound = self.source.play_audio(
            start, end, on_started=lambda: self.events.put(("audiostarted", token)))
        self.play_shown_index = -1
        if not self.play_sound:
            self._start_play_clock(token)
            return

        # Silent until ffplay says it is open. It takes about 870ms here and
        # differs by machine, so starting the picture on a fixed guess put it
        # most of a second ahead of the sound. The first frame goes up now so
        # the wait does not look like nothing happening.
        self._show_play_frame(0)
        self.play_shown_index = 0
        self.play_clock_job = self.window.after(
            int(self.AUDIO_WAIT_MAX * 1000), self._start_play_clock, token)

    def _start_play_clock(self, token):
        """Begin advancing frames. Whichever signal arrives first wins."""
        if token != self.play_token or self.play_started is not None:
            return
        if self.play_clock_job is not None:
            try:
                self.window.after_cancel(self.play_clock_job)
            except Exception:  # noqa: BLE001
                pass
            self.play_clock_job = None
        self.play_started = time.monotonic()
        self._advance_playback()

    def _advance_playback(self):
        """Show whichever frame the clock says we are on.

        The index comes from elapsed time rather than from counting ticks, so
        a late tick skips a frame instead of letting the picture drift behind
        the sound for the rest of the clip.
        """
        if not self.alive or not self.play_frames or self.play_started is None:
            return
        elapsed = time.monotonic() - self.play_started
        if elapsed < 0:
            self.play_job = self.window.after(20, self._advance_playback)
            return

        index = int(elapsed * self.play_fps)
        if index >= len(self.play_frames):
            self.stop_playback()
            return

        if index == self.play_shown_index:
            # Woken before this frame was actually due -- after() rounds to
            # whole milliseconds and can fire a touch early. Redrawing the
            # frame already on screen costs a PNG decode for no change, so
            # just aim at the next one again.
            target = self.play_started + (index + 1) / float(self.play_fps)
            delay = int((target - time.monotonic()) * 1000)
            self.play_job = self.window.after(max(1, delay), self._advance_playback)
            return

        self.play_shown_index = index
        self._show_play_frame(index)
        position = index / float(self.play_fps)
        self.playhead = min(self.play_start + position, self.duration)
        self._draw_playhead()
        self.frame_label.configure(text=clock_label(self.playhead, 1))
        # Where we are inside the clip, not inside the video: the playhead on
        # the strip already carries the second reading, and a bare "playing"
        # left the only answer to "how much is left" in a 2px amber line.
        # "(no sound)" rides along rather than being announced once and
        # forgotten, because Play is often pressed before the caption is read.
        self.set_play_note("%s / %s%s" % (clock_label(position),
                                          clock_label(self.play_span),
                                          "" if self.play_sound else "   (no sound)"))
        # Decode the next frame now, out of the slack before it is due, so the
        # tick that has to show it only has to hand it to the label.
        self._prefetch_play_frame(index + 1)

        # Aim at when that frame is due, rather than waiting a fixed interval
        # from here. Measured: adding after(83) *after* ~8ms of work made every
        # cycle 93ms, so the clock ran ahead of the picture and paid for it by
        # skipping frames -- 9 of 120 dropped, which is what judder looks like.
        target = self.play_started + (index + 1) / float(self.play_fps)
        delay = int((target - time.monotonic()) * 1000)
        self.play_job = self.window.after(max(1, delay), self._advance_playback)

    def _show_play_frame(self, index):
        """Put frame `index` on screen, using the prefetched one when it fits."""
        try:
            if self.play_ahead_index == index and self.play_ahead is not None:
                self.play_image = self.play_ahead
            else:
                self.play_image = tk.PhotoImage(file=self.play_frames[index])
            self.play_ahead, self.play_ahead_index = None, -1
            self.picture.configure(image=self.play_image, text="")
        except Exception:  # noqa: BLE001
            pass

    def _prefetch_play_frame(self, index):
        if not (0 <= index < len(self.play_frames)):
            return
        try:
            self.play_ahead = tk.PhotoImage(file=self.play_frames[index])
            self.play_ahead_index = index
        except Exception:  # noqa: BLE001
            self.play_ahead, self.play_ahead_index = None, -1

    def stop_playback(self, keep_note=False):
        # Anything still decoding belongs to the run being stopped, so the
        # token moves and the frames are dropped when they arrive.
        self.play_token += 1
        if self.play_job is not None:
            try:
                self.window.after_cancel(self.play_job)
            except Exception:  # noqa: BLE001
                pass
        self.play_job = None
        self.play_frames = []
        self.play_ahead, self.play_ahead_index = None, -1
        self.play_shown_index = -1
        self.play_started = None
        self.preparing = False
        self.source.stop()
        if not self.alive:
            return
        try:
            self._busy_cursor(False)
            self.play_button.configure(text="Play")
            if not keep_note:
                self.set_play_note(self.play_hint())
            self.show_frame(self.playhead)
            self._draw_playhead()
        except Exception:  # noqa: BLE001
            pass

    def set_play_note(self, text, bad=False):
        """The one caption everything about playback speaks through.

        Guarded against repeats because it is written twelve times a second
        while a clip runs and most of those ticks carry the same string, and
        because the style goes with the text: a failure has to be able to
        colour itself without a second label appearing beside the first.
        """
        style = "Warning.Muted.TLabel" if bad else "Muted.TLabel"
        if (text, style) == self.play_note_state:
            return
        self.play_note_state = (text, style)
        self.play_note.configure(text=text, style=style)

    def _busy_cursor(self, busy):
        """Preparing, said the one way that costs no pixels.

        There is no fraction to show -- ffmpeg is not asked for one -- and a
        bar with nothing behind it would be the progress that is not real.
        """
        try:
            self.window.configure(cursor="watch" if busy else "")
        except tk.TclError:
            pass

    def play_hint(self):
        """The caption with nothing playing: silence unless there is news.

        "plays here, with sound" described the implementation to someone who
        had not asked. What is worth saying before Play is pressed is the
        case where nothing will be heard, and which of the two reasons it is.
        """
        if not self.source.has_audio:
            return "no sound in this copy"
        if not ffmpeg_tool("ffplay", self.source.ffmpeg_dir):
            return "no sound - ffplay is not installed"
        return ""

    # ------------------------------------------------------------- finishing
    def accept(self):
        whole = self.t_in <= 0.5 and self.t_out >= self.duration - 0.5
        self.result = (None, None) if whole else (self.t_in, self.t_out)
        self._shut()

    def cancel(self):
        self.result = None
        self._shut()

    def _shut(self):
        self.stop_playback(keep_note=True)
        self.alive = False  # stops pump() rescheduling onto a dead window
        try:
            self.window.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.source.close()
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
