"""The trim window: filmstrip, handles, transcript."""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .captions import fetch_transcript
from .config import EDITOR_PALETTE
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
    # The ceiling is for portrait video, whose 160x284 tiles would otherwise
    # hand the strip 45% of the window and leave the transcript a sliver.
    STRIP_HEIGHT = 60
    STRIP_MAX = 120
    STILL_MAX = 300         # how tall the preview may be before it is cut down
    RULER_HEIGHT = 20
    TILE_SUBSAMPLE = 3      # a 480x270 still becomes a 160x90 tile
    GRAB_SLOP = 7           # pixels either side of a handle that still grab it
    HANDLE_W = 10           # drawn inside the selection, so it never half-clips
    MIN_SPAN = 0.5          # shorter than this is a slip of the mouse, not a clip
    LINE_PADDING = 0.35     # air around a caption, which is timed to the word
    # How long to wait for ffplay to report itself before starting the
    # picture anyway. Its start-up is about a second, 1.8s measured with
    # Windows' process cost; this is only the ceiling for a build that never
    # prints. Windows itself plays through winsound and never waits on it.
    AUDIO_WAIT_MAX = 2.5
    WARM_DELAY = 250        # ms of a still selection before its frames start
    FIRST_FRAME_WAIT = 1.5  # s a cold Play waits for ffmpeg's first frame
    SETTLE_DELAY = 150      # ms the playhead holds still before the exact frame
    # An end this close to the edge of the video is the edge. The boxes show
    # tenths, so anything coarser threw away a cut the boxes were showing.
    WHOLE_SLACK = 0.05
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
        self.settle_job = None
        # Playback: the frames for the current span, the clock it runs on, and
        # the pending after() ids so stopping is immediate.
        self.play_stream = None
        self.play_fps = 0
        self.play_started = None   # None until the sound is actually up
        self.play_start = 0.0
        self.play_span = 0.0
        self.play_sound = False
        self.play_job = None
        self.play_clock_job = None
        self.play_image = None
        self.play_still = None     # the filmstrip still standing in, if any
        self.play_shown_index = -1
        # The next Play's frames, decoding ahead of the press, and the span
        # they were started for.
        self.warm = None
        self.warm_span = None
        self.warm_job = None
        # Which run of playback we are on. A "sound started" that arrives
        # after Stop carries a stale token and is dropped.
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
        # 880x620 would have clipped the buttons off the bottom. 1040 wide
        # since the options column moved left of the strip: at 880 it left the
        # filmstrip about 550px, a third less to aim with than it had.
        self.window.minsize(self.theme.scaled(1040), self.theme.scaled(660))
        # wait_window alone runs a nested loop but leaves the main window
        # clickable, so Download could be pressed mid-trim and queue the old
        # times, which apply_clip_result would then overwrite.
        self.window.grab_set()
        self.pump()  # started on the main thread, and reschedules itself there
        threading.Thread(target=self._prepare_worker, daemon=True).start()

    # ---------------------------------------------------------------- layout
    def _build(self):
        tone = EDITOR_PALETTE
        px = self.theme.px
        self.window.configure(background=tone["bg"])

        # An editor's layout: the picture large on black with its transport
        # under it, the words in a panel beside it, and the timeline across
        # the whole width at the foot, where the cut is actually made.
        top = ttk.Frame(self.window, style="Editor.TFrame", padding=(px("lg"), px("sm")))
        self.title_label = ttk.Label(top, text="Reading the video...",
                                     style="Editor.Section.TLabel")
        self.title_label.pack(side="left")

        middle = ttk.Frame(self.window, style="Editor.TFrame")
        viewer = ttk.Frame(middle, style="Editor.TFrame")
        # A fixed stage rather than one sized by the picture: the label is
        # text until the first still arrives, and the timeline under it would
        # jump the moment the video loaded.
        monitor = tk.Frame(viewer, background=tone["monitor"],
                           width=self.theme.scaled(560), height=self.theme.scaled(320))
        monitor.pack_propagate(False)
        self.picture = tk.Label(monitor, background=tone["monitor"],
                                foreground=tone["muted"], font=self.theme.body,
                                text="Fetching a small copy to scrub through...")
        self.picture.pack(fill="both", expand=True)

        # The transport. The clock is the moment on screen, and only that; the
        # second number appears only while the still is the rounded one.
        transport = ttk.Frame(viewer, style="Editor.TFrame", padding=(px("md"), px("sm")))
        self.frame_label = ttk.Label(transport, text="0:00.0", style="Editor.Timecode.TLabel")
        self.frame_label.pack(side="left")
        self.length_label = ttk.Label(transport, text="", style="Editor.Muted.TLabel")
        self.length_label.pack(side="right")
        # Fixed width, because the label changes: "Play" and "Stop" have to
        # occupy the same box or the button moves when playback starts.
        self.play_button = self._button(transport, "Play", self.play, width=8,
                                        state="disabled")
        self.play_button.pack(side="left", expand=True)
        transport.pack(side="bottom", fill="x")
        monitor.pack(fill="both", expand=True)

        panel = ttk.Frame(middle, style="Editor.TFrame", padding=px("md"))
        finder = ttk.Frame(panel, style="Editor.TFrame")
        finder.pack(fill="x")
        ttk.Label(finder, text="Transcript", style="Editor.Section.TLabel").pack(side="left")
        self.search_var = tk.StringVar(value="")
        self.search_entry = self._entry(finder, self.search_var, 16)
        self.search_entry.pack(side="right", ipady=px("xs"))
        ttk.Label(finder, text="Search", style="Editor.Muted.TLabel").pack(
            side="right", padx=(0, px("sm")))
        self.search_var.trace_add("write", self.filter_transcript)

        holder = ttk.Frame(panel, style="Editor.TFrame")
        holder.pack(fill="both", expand=True, pady=(px("sm"), 0))
        # A caption line is longer than this panel, and the sentence that
        # decides the cut was the one running off the edge. Packed first so
        # it keeps its strip at the bottom.
        xbar = ttk.Scrollbar(holder, orient="horizontal",
                             style="Editor.Horizontal.TScrollbar")
        xbar.pack(side="bottom", fill="x")
        bar = ttk.Scrollbar(holder, style="Editor.Vertical.TScrollbar")
        bar.pack(side="right", fill="y")
        self.transcript_view = ttk.Treeview(
            holder, columns=("text",), show="tree", height=6,
            yscrollcommand=bar.set, xscrollcommand=xbar.set,
            style="Editor.Transcript.Treeview")
        self.transcript_view.column("#0", width=self.theme.scaled(56), stretch=False)
        self.transcript_view.column("text", width=self.theme.scaled(260))
        self.transcript_view.pack(side="left", fill="both", expand=True)
        bar.configure(command=self.transcript_view.yview)
        xbar.configure(command=self.transcript_view.xview)
        self.transcript_view.bind("<<TreeviewSelect>>", self.on_transcript_pick)
        self.transcript_view.bind("<Double-1>", lambda _e: self.clip_selected_lines())

        # Blank until something is actually happening, and on its own line:
        # the panel is narrower than the sentence and the button together.
        self.transcript_note = ttk.Label(panel, text="", style="Editor.Muted.TLabel",
                                         wraplength=self.theme.scaled(300), justify="left")
        self.transcript_note.pack(anchor="w", pady=(px("sm"), 0))
        self.clip_line_button = self._button(panel, "Clip these lines",
                                             self.clip_selected_lines, state="disabled")
        self.clip_line_button.pack(anchor="e", pady=(px("sm"), 0))

        deck = ttk.Frame(self.window, style="Editor.TFrame", padding=(px("lg"), px("md")))
        marks = ttk.Frame(deck, style="Editor.TFrame")
        marks.pack(fill="x")
        # Typable, not readonly: anyone who already knows the timestamp types
        # it. "Mark in" and "Mark out" are what every editor calls these, and
        # I and O are the keys every editor gives them.
        self.in_var = tk.StringVar(value="")
        self.out_var = tk.StringVar(value="")
        for label, var, command, name in (("IN", self.in_var, self.set_in, "Mark in"),
                                          ("OUT", self.out_var, self.set_out, "Mark out")):
            ttk.Label(marks, text=label, style="Editor.Muted.TLabel").pack(side="left")
            entry = self._entry(marks, var, 9)
            entry.pack(side="left", padx=(px("sm"), px("sm")), ipady=px("xs"))
            self._button(marks, name, command).pack(side="left", padx=(0, px("xl")))
            if label == "IN":
                self.in_entry = entry
            else:
                self.out_entry = entry
        for entry, which in ((self.in_entry, "in"), (self.out_entry, "out")):
            entry.bind("<Return>", lambda _e, w=which: self.commit_time(w))
            entry.bind("<FocusOut>", lambda _e, w=which: self.commit_time(w))
        self.whole_button = self._button(marks, "Whole video", self.reset, state="disabled")
        self.whole_button.pack(side="right")

        self.canvas = tk.Canvas(deck, height=self.strip + self.ruler, takefocus=1,
                                highlightthickness=1, highlightbackground=tone["line"],
                                highlightcolor=tone["focus"],
                                background=self.theme.c("sunken"))
        self.canvas.pack(fill="x", pady=(px("md"), 0))
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        notes = ttk.Frame(deck, style="Editor.TFrame")
        notes.pack(fill="x", pady=(px("sm"), 0))
        self.play_note = ttk.Label(notes, text="", style="Editor.Muted.TLabel")
        self.play_note.pack(side="left")
        self.hint_label = ttk.Label(
            notes, text="Drag the handles, or click the strip to move the playhead. "
                        "Arrows nudge, I and O set the ends, space plays.",
            style="Editor.Muted.TLabel")
        self.hint_label.pack(side="right")

        # The two decisions that close the window. Drawn in the top bar, but
        # Tab follows stacking order, so the bar is lifted to the end of it:
        # they are reached after everything they decide.
        cancel = self._button(top, "Cancel", self.cancel)
        self.use_button = self._button(top, "Use this clip", self.accept, primary=True,
                                       state="disabled")
        self.use_button.pack(side="right")
        cancel.pack(side="right", padx=(0, px("sm")))

        top.lift()
        top.pack(fill="x")
        self._rule(self.window)
        deck.pack(side="bottom", fill="x")
        self._rule(self.window, side="bottom")
        middle.pack(fill="both", expand=True)
        panel.pack(side="right", fill="y")
        self._rule(middle, side="right", vertical=True)
        viewer.pack(side="left", fill="both", expand=True)

        for key in ("<Left>", "<Right>", "<Shift-Left>", "<Shift-Right>",
                    "<i>", "<o>", "<I>", "<O>", "<space>"):
            self.window.bind(key, self.on_key)

    def _button(self, parent, text, command, primary=False, **options):
        """A flat button in the editor's colours.

        A classic Tk button, because a ttk one on Windows and macOS is drawn by
        the system and ignores every colour it is given. The focus ring is the
        highlight border, so the keyboard can still see where it is.
        """
        tone = EDITOR_PALETTE
        fill, over = ((tone["accent"], tone["accent_hover"]) if primary
                      else (tone["raised"], tone["raised_hover"]))
        if options.get("state") == "disabled":
            # Tk has no disabled background, and muted text on the accent blue
            # read at 2.11:1 for the whole of the load. Blue arrives with use.
            fill = tone["raised"]
        ink = tone["on_accent"] if primary else tone["text"]
        return tk.Button(parent, text=text, command=command, relief="flat",
                         borderwidth=0, background=fill, foreground=ink,
                         activebackground=over, activeforeground=ink,
                         disabledforeground=tone["muted"], highlightthickness=1,
                         highlightbackground=tone["bg"], highlightcolor=tone["focus"],
                         font=self.theme.action if primary else self.theme.body,
                         padx=self.theme.px("md"), pady=self.theme.px("xs"),
                         cursor="hand2", **options)

    def _entry(self, parent, variable, width):
        tone = EDITOR_PALETTE
        return tk.Entry(parent, textvariable=variable, width=width, relief="flat",
                        borderwidth=0, background=tone["field"], foreground=tone["text"],
                        insertbackground=tone["text"], disabledbackground=tone["bg"],
                        disabledforeground=tone["muted"], selectbackground=tone["accent"],
                        selectforeground=tone["on_accent"], highlightthickness=1,
                        highlightbackground=tone["edge"], highlightcolor=tone["focus"],
                        font=self.theme.mono)

    def _rule(self, parent, side="top", vertical=False):
        line = tk.Frame(parent, background=EDITOR_PALETTE["line"],
                        **({"width": 1} if vertical else {"height": 1}))
        line.pack(side=side, fill="y" if vertical else "x")

    # --------------------------------------------------------------- loading
    def _prepare_worker(self):
        try:
            self.source.prepare(self._stage_from_worker)
            if not self.source.cancelled:
                self.events.put(("ready", None))
        # KeyboardInterrupt is how the download hook stops a closed window; let
        # through, it printed a traceback from a thread nobody was watching.
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
            if not self.source.cancelled:
                self.events.put(("failed", str(exc)))
        finally:
            if self.source.cancelled:
                # close() ran while the download still held its .part file
                # open, so the folder outlived it. It is free now.
                self.source.discard()

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
        self._set_marks(t_in=self.t_in)
        self.playhead = self.t_in
        self.title_label.configure(text=self.source.title)
        # The window title is one of the few things a screen reader announces,
        # so spending it on a raw URL wastes the cheapest accessible channel
        # this window has.
        self.window.title("Trim - " + self.source.title[:60])
        self.picture.configure(text="", width=0, height=0)
        self.play_button.configure(state="normal")
        self.use_button.configure(state="normal", background=EDITOR_PALETTE["accent"])
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
        self._set_marks(t_in=0.0)
        self._set_marks(t_out=picked[-1]["end"] + self.LINE_PADDING)
        self._set_marks(t_in=picked[0]["start"] - self.LINE_PADDING)
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
        # of a 480px-wide still, so it does not grow with the display, and a
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
        self._schedule_warm()
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
        # Tenths, like the boxes: a half-second clip read "clip is 0:00".
        self.length_label.configure(
            text="the whole video, %s" % format_duration(self.duration) if self._is_whole()
            else "clip is %s of %s" % (clock_label(span, 1),
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
        """Shrink a portrait still until it fits the monitor.

        The stills are 480px wide whatever the shape of the video, so a 9:16
        one comes back 853px tall -- far taller than the monitor, which is a
        fixed stage. Tk only resizes by whole numbers, so this halves and
        thirds.
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
        # A held arrow key settles thirty times a second, and one ffmpeg each
        # had six alive at once. Only a playhead that holds still is decoded.
        if self.settle_job is not None:
            try:
                self.window.after_cancel(self.settle_job)
            except Exception:  # noqa: BLE001
                pass
        self.settle_job = self.window.after(self.SETTLE_DELAY, self._settle_now,
                                            seconds, token)

    def _settle_now(self, seconds, token):
        self.settle_job = None
        if token != self.settle_token or not self.alive:
            return

        def worker():
            path = self.source.exact_frame(seconds)
            if not path:
                return
            if token == self.settle_token and not self.source.cancelled:
                self.events.put(("exact", (path, seconds, token)))
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_exact(self, path, seconds, token):
        try:
            if token != self.settle_token:
                return  # still removed below: a stale frame is a stale file
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
            self._set_marks(t_in=seconds)
            self.playhead = self.t_in
        elif self.dragging == "out":
            self._set_marks(t_out=seconds)
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
        # A focused button already answers Space, and the transcript list
        # answers Space and the arrows. Doing the editor's thing as well was
        # two actions for one key: Play pressed twice is no Play at all.
        if key == "space" and isinstance(focused, (tk.Button, ttk.Button)):
            return None
        if key in ("space", "left", "right") and isinstance(focused, ttk.Treeview):
            return None
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

    def _set_marks(self, t_in=None, t_out=None):
        """Move an end of the clip, keeping it inside the video and at least
        MIN_SPAN long.

        Every way of setting an end comes through here. The drag, I and O, the
        two boxes and the caption lines each kept their own arithmetic, and
        between them a clip could start at -0.2s or end 0.4s past the last
        frame -- and that is what went back to the main window.
        """
        if self.play_stream is not None:
            # What is playing is the clip as it was when Play was pressed, and
            # it kept playing that while the handle was dragged elsewhere.
            self.stop_playback()
        span = min(self.MIN_SPAN, self.duration)
        if t_in is not None:
            self.t_out = min(max(self.t_out, span), self.duration)
            self.t_in = min(max(t_in, 0.0), self.t_out - span)
        if t_out is not None:
            self.t_in = min(max(self.t_in, 0.0), self.duration - span)
            self.t_out = min(max(t_out, self.t_in + span), self.duration)

    def _is_whole(self):
        return (self.t_in <= self.WHOLE_SLACK
                and self.t_out >= self.duration - self.WHOLE_SLACK)

    def set_in(self):
        if self.duration <= 0:
            return
        self._set_marks(t_in=self.playhead)
        self.redraw()

    def set_out(self):
        if self.duration <= 0:
            return
        self._set_marks(t_out=self.playhead)
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
            self._set_marks(t_in=seconds)
            self.playhead = self.t_in
        else:
            self._set_marks(t_out=seconds)
            self.playhead = self.t_out
        self.redraw()
        self.show_frame(self.playhead)
        self.settle(self.playhead)
        return "break"

    def reset(self):
        if self.duration <= 0:
            return
        self._set_marks(t_out=self.duration)
        self._set_marks(t_in=0.0)
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
        if self.play_stream is not None:
            self.stop_playback()
            return

        start, end = self.t_in, self.t_out
        self.play_token += 1
        token = self.play_token
        if self.warm is not None and self.warm_span == (start, end):
            self.play_stream = self.warm
        else:
            self._drop_warm()
            self.play_stream = self.source.stream_frames(start, end)
        self.warm = None
        if self.play_stream is None:
            self.set_play_note("ffmpeg could not decode that span", bad=True)
            return
        self.play_start = start
        self.play_span = max(0.0, end - start)
        self.play_fps = self.source.PLAY_FPS
        self.play_shown_index = -1
        self.play_button.configure(text="Stop", state="normal")
        self._await_first_frame(token, time.monotonic() + self.FIRST_FRAME_WAIT)

    def _await_first_frame(self, token, deadline):
        """Hold the start until ffmpeg has a frame, or for FIRST_FRAME_WAIT.

        A selection played the moment it is made has no frames yet, because
        ffmpeg takes about a second to start on Windows, and starting the
        sound anyway played a half-second clip entirely over filmstrip stills.
        Only this case waits: a selection that has sat still for WARM_DELAY is
        already decoding and starts at once.
        """
        self.play_clock_job = None
        if token != self.play_token or self.play_stream is None:
            return
        if not self.play_stream.ready() and time.monotonic() < deadline:
            self.set_play_note("starting...")
            self.play_clock_job = self.window.after(20, self._await_first_frame,
                                                    token, deadline)
            return
        start, end = self.play_start, self.play_start + self.play_span
        # Sound is optional: a proxy without an audio track, or a machine
        # without anything to play it, still gets the picture. Set before the
        # call, because winsound starts the clock from inside it.
        self.play_sound = True
        if not self.source.play_audio(start, end,
                                      on_started=lambda: self._sound_started(token)):
            self.play_sound = False
            self._start_play_clock(token)
        elif self.play_started is None:
            # ffplay, still opening: most of a second, and different on every
            # machine, so the picture waits for it rather than for a guess.
            self.set_play_note("starting the sound...")
            self._show_still(start)
            self.play_clock_job = self.window.after(
                int(self.AUDIO_WAIT_MAX * 1000), self._start_play_clock, token)

    def _schedule_warm(self):
        """Start decoding the selection a moment after it stops changing.

        Starting ffmpeg costs about a second on Windows. Paid on Play, that was
        a second of filmstrip stills before the real picture; paid here, while
        nobody is waiting, the first frames are already queued when Play is
        pressed. The delay keeps a drag from starting a process per pixel.
        """
        if self.warm_job is not None:
            try:
                self.window.after_cancel(self.warm_job)
            except Exception:  # noqa: BLE001
                pass
        self.warm_job = self.window.after(self.WARM_DELAY, self._warm)

    def _warm(self):
        self.warm_job = None
        if not self.alive or self.duration <= 0 or self.play_stream is not None:
            return
        span = (self.t_in, self.t_out)
        if self.warm is not None and self.warm_span == span:
            return
        self._drop_warm()
        self.warm, self.warm_span = self.source.stream_frames(*span), span

    def _drop_warm(self):
        if self.warm is not None:
            self.warm.close()
        self.warm, self.warm_span = None, None
        if self.warm_job is not None and not self.alive:
            try:
                self.window.after_cancel(self.warm_job)
            except Exception:  # noqa: BLE001
                pass
            self.warm_job = None

    def _sound_started(self, token):
        """The sound is going: start the picture's clock with it.

        winsound calls this from play() itself, and going through the event
        queue there would cost up to a whole pump interval of picture behind
        sound. ffplay calls it from a reader thread, which must not touch Tk.
        """
        if threading.current_thread() is threading.main_thread():
            self._start_play_clock(token)
        else:
            self.events.put(("audiostarted", token))

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
        if not self.alive or self.play_stream is None or self.play_started is None:
            return
        elapsed = time.monotonic() - self.play_started
        if elapsed >= self.play_span:
            self.stop_playback()
            return

        index = int(elapsed * self.play_fps)
        if index != self.play_shown_index:
            frame = self.play_stream.take(index)
            if frame is not None:
                self._show_play_frame(frame)
            elif self.play_stream.taken <= index:
                # ffmpeg is still starting -- about a second on Windows. The
                # filmstrip's stills stand in, so the sound never waits for it.
                self._show_still(self.play_start + elapsed)
            self.play_shown_index = index
            position = index / float(self.play_fps)
            self.playhead = min(self.play_start + position, self.duration)
            self._draw_playhead()
            self.frame_label.configure(text=clock_label(self.playhead, 1))
            # Where we are inside the clip, not inside the video: the playhead
            # on the strip already carries the second reading. "(no sound)"
            # rides along because Play is often pressed before it is read.
            self.set_play_note("%s / %s%s" % (clock_label(position),
                                              clock_label(self.play_span),
                                              "" if self.play_sound else "   (no sound)"))

        # Aim at when the next frame is due, rather than waiting a fixed
        # interval from here: a fixed after(83) on top of the work made every
        # cycle longer than a frame, and the picture paid for it in skips.
        target = self.play_started + (index + 1) / float(self.play_fps)
        delay = int((target - time.monotonic()) * 1000)
        self.play_job = self.window.after(max(1, delay), self._advance_playback)

    def _show_play_frame(self, frame):
        try:
            self.play_image = tk.PhotoImage(data=frame, format="ppm")
            self.picture.configure(image=self.play_image, text="")
        except Exception:  # noqa: BLE001
            pass

    def _show_still(self, seconds):
        """The filmstrip's nearest still, decoded only when it changes."""
        path = self.source.frame_for(seconds)
        if not path or path == self.play_still:
            return
        self.play_still = path
        try:
            self.play_image = self._fit_still(tk.PhotoImage(file=path))
            self.picture.configure(image=self.play_image, text="")
        except Exception:  # noqa: BLE001
            pass

    def stop_playback(self, keep_note=False):
        # Anything still on its way belongs to the run being stopped, so the
        # token moves and a late "sound started" is dropped when it arrives.
        self.play_token += 1
        for job in (self.play_job, self.play_clock_job):
            if job is not None:
                try:
                    self.window.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
        self.play_job = None
        self.play_clock_job = None
        if self.play_stream is not None:
            self.play_stream.close()
            self.play_stream = None
        self.play_shown_index = -1
        self.play_started = None
        self.play_still = None
        self.source.stop()
        if not self.alive:
            return
        self._schedule_warm()
        try:
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
        style = "Editor.Warning.Muted.TLabel" if bad else "Editor.Muted.TLabel"
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
        if not self.source.can_sound():
            return "no sound - ffplay is not installed"
        return ""

    # ------------------------------------------------------------- finishing
    def accept(self):
        self.result = (None, None) if self._is_whole() else (self.t_in, self.t_out)
        self._shut()

    def cancel(self):
        self.result = None
        self._shut()

    def _shut(self):
        self.stop_playback(keep_note=True)
        self.alive = False  # stops pump() rescheduling onto a dead window
        self._drop_warm()
        try:
            self.window.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.source.close()
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
