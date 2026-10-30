"""Layout facts that a screenshot would catch and arithmetic would not.

Every test here needs a real window, because what is being checked is where
Tk put something and how big it made it. They skip rather than fail where
there is no display, in the same shape as the last test in
test_palette_contrast.py.
"""

import threading
import time

import pytest


@pytest.fixture(scope="session")
def has_display():
    """Asked once, and asked more than once before giving up.

    Two things are being worked around. A root per test on top of the window
    under test doubles the interpreters a run goes through, and part way in
    Tk on Windows starts reporting "couldn't read file init.tcl: No error" --
    an OS read that failed with no reason attached, which a moment later
    succeeds. Skipping on the first of those would mean this file silently
    stops guarding anything on the machine it was written for.
    """
    tkinter = pytest.importorskip("tkinter")
    problem = None
    for _attempt in range(3):
        try:
            probe = tkinter.Tk()
        except Exception as failure:  # noqa: BLE001 -- any Tcl failure will do
            problem = failure
            time.sleep(0.2)
            continue
        probe.destroy()
        return True
    pytest.skip("no display for Tk: %s" % problem)


@pytest.fixture(scope="module")
def built(anydl_module, has_display):
    """One window for the module.

    Building it per test means a Tcl interpreter per test, and on Windows the
    fifth or sixth one starts failing to source init.tcl -- an error that has
    nothing to do with what is being tested. The window is hidden: a run of
    the suite should not flash a window on somebody's screen.
    """
    anydl_module._load_tk()
    app = anydl_module.AnydlApp()
    app.window.withdraw()
    app.window.update_idletasks()
    try:
        yield app
    finally:
        app.window.destroy()


@pytest.fixture
def window(built):
    """The shared window, put back the way it was found."""
    yield built
    built.hush()
    built.preview_line.pack_forget()
    built.preview_btn.configure(state="normal")
    built.mode_var.set("video")
    built.urls_box.delete("1.0", "end")
    built.window.update_idletasks()


def y_of(app, widget):
    return widget.winfo_rooty() - app.window.winfo_rooty()


def test_status_column_fits_the_longest_status(anydl_module, window):
    """"Downloading" is 88px of mono in a column that used to be 90px wide,
    and the Treeview pads a cell either side, so the word lost its last
    letter for most of a download's life."""
    theme = window.theme
    widest = max(theme.measure(theme.mono, spec["label"])
                 for spec in anydl_module.STATUS_TABLE.values())

    assert int(window.tree.column("status", "width")) >= widest + theme.px("sm")


def test_queue_rows_clear_the_pointer_target_minimum(window):
    """A row selects, opens a folder on a double click and carries the
    context menu, so it is a target: SC 2.5.8 wants 24px."""
    height = window.theme.style.lookup("Queue.Treeview", "rowheight")

    assert int(height) >= 24


def test_inline_message_lands_under_the_link_box(window):
    """It is about the box, the box is what gets the focus with it, and pack
    with no anchor appends -- which put it under the Download button."""
    window.say("Paste a link first.")
    window.window.update_idletasks()

    assert y_of(window, window.message) > y_of(window, window.urls_box)
    assert y_of(window, window.message) < y_of(window, window.download_btn)


def test_reading_line_lands_under_the_status_line(window, monkeypatch):
    """Same appending mistake, other end of the window: "Reading ..." was
    drawn below the log box, four regions away from the button that starts
    it."""
    monkeypatch.setattr(window, "_preview_worker", lambda *_a, **_k: None)
    window.urls_box.insert("1.0", "https://example.com/watch?v=x")
    window.preview()
    window.window.update_idletasks()

    assert y_of(window, window.preview_line) > y_of(window, window.bar)
    assert y_of(window, window.preview_line) < y_of(window, window.tree)


def test_gated_labels_keep_their_size(window):
    """The label of a control that does not apply is greyed, not shrunk: the
    small font made it narrower and shorter, and every control to its right
    moved when the mode changed."""
    before = [(w.winfo_reqwidth(), w.winfo_reqheight())
              for w in (window.quality_label, window.format_label,
                        window.bitrate_label, window.subs_label)]
    window.mode_var.set("audio")
    window.window.update_idletasks()
    after = [(w.winfo_reqwidth(), w.winfo_reqheight())
             for w in (window.quality_label, window.format_label,
                       window.bitrate_label, window.subs_label)]

    assert before == after


def test_a_banner_with_no_action_shows_no_action_button(window):
    """The strip is built hidden, so its buttons report themselves unmapped
    while pack is already managing them -- and the "no ffmpeg" message, which
    asks for no button, came up offering to update yt-dlp instead."""
    window.show_banner("ffmpeg is not installed.", None)
    window.window.update_idletasks()
    assert window.update_btn.winfo_manager() == ""

    window.show_banner("A newer yt-dlp is available.", "Update now")
    window.window.update_idletasks()
    assert window.update_btn.winfo_manager() == "pack"

    window.show_banner("ffmpeg is not installed.", None)
    window.window.update_idletasks()
    assert window.update_btn.winfo_manager() == ""
    window.hide_banner()


def test_the_form_labels_share_one_column(window):
    """Preset, Output and Save to are three rows of the same form, and their
    controls have to start on the same vertical line."""
    window.window.update_idletasks()
    # The first control of each of the three rows: the Music radio, the Video
    # radio and the destination entry, found through the label that names the
    # row rather than by index, so adding a row does not silently skip it.
    starts = {}
    for row in window.top.winfo_children():
        kids = row.winfo_children() if row.winfo_class() == "TFrame" else []
        if len(kids) < 2 or kids[0].winfo_class() != "TLabel":
            continue
        name = str(kids[0].cget("text"))
        if name in ("Preset:", "Output:", "Save to:"):
            starts[name] = kids[1].winfo_rootx()

    assert len(starts) == 3, starts
    assert len(set(starts.values())) == 1, starts


def test_a_long_title_and_a_long_error_do_not_break_the_row_or_widen_the_window(window):
    """The row shows one line and lets the column clip it; the window must
    not grow just because one link has an enormous title or error."""
    before_width = window.window.winfo_reqwidth()
    long_label = "A" + "very " * 60 + "long title"
    long_detail = "ERROR: " + "x" * 500

    window.rows[999999] = type("Row", (), {
        "uid": 999999, "status": "Failed", "progress": 0.0,
        "detail": long_detail, "label": long_label, "summary": "MP4 · Best",
    })()
    window.tree.insert("", "end", iid="999999", text=long_label,
                       values=("MP4 · Best", "Queued", ""), tags=("Queued",))
    try:
        window.refresh_row(window.rows[999999])
        window.window.update_idletasks()

        assert window.tree.item("999999", "text") == long_label
        values = window.tree.item("999999", "values")
        assert long_detail in values[2]
        # A Treeview cell clips instead of growing the column, so one absurd
        # row must not be able to widen the whole window.
        assert window.window.winfo_reqwidth() == before_width
    finally:
        window.tree.delete("999999")
        window.rows.pop(999999, None)


def test_fit_to_screen_reserves_more_chrome_at_150_percent(window):
    """_fit_to_screen's ceiling leaves room for the title bar and taskbar,
    and that allowance is scaled -- so the same short screen should leave
    strictly less room for content at 150% than at 100%."""
    orig_screenheight = window.window.winfo_screenheight
    orig_screenwidth = window.window.winfo_screenwidth
    orig_scale = window.theme.scale
    orig_minsize = window.window.minsize()
    try:
        window.window.winfo_screenheight = lambda: 768
        window.window.winfo_screenwidth = lambda: 1366

        window.theme.scale = 1.0
        window._fit_to_screen()
        window.window.update_idletasks()
        min_w_100, min_h_100 = window.window.minsize()

        window.theme.scale = 1.5
        window._fit_to_screen()
        window.window.update_idletasks()
        min_w_150, min_h_150 = window.window.minsize()

        assert 0 < min_h_150 <= min_h_100 <= 768
        assert 0 < min_w_150 <= 1366
        assert 0 < min_w_100 <= 1366
    finally:
        window.window.winfo_screenheight = orig_screenheight
        window.window.winfo_screenwidth = orig_screenwidth
        window.theme.scale = orig_scale
        window.window.minsize(*orig_minsize)
        window._fit_to_screen()
        window.window.update_idletasks()


def test_clearing_the_cookie_file_while_the_panel_is_closed_is_seen_when_reopened(window):
    """Same shape of bug as the banner's update button: winfo_ismapped() asks
    whether the whole window chain is on screen, not whether this widget has
    a geometry manager, so it comes back False while the panel that holds the
    button is folded -- even though the button itself is still packed."""
    try:
        window.open_extras()
        window.cookie_file_var.set("C:/fake/cookies.txt")
        window.show_cookie_file()
        window.window.update_idletasks()
        assert window.cookie_clear.winfo_manager() == "pack"

        window.disclosure_var.set(False)
        window.toggle_extras()
        window.window.update_idletasks()

        window.clear_cookie_file()
        window.window.update_idletasks()
        assert window.cookie_file_var.get() == ""

        window.open_extras()
        window.window.update_idletasks()
        assert window.cookie_clear.winfo_manager() == "", (
            "the 'clear' button is still packed after the cookie file was "
            "cleared, because show_cookie_file() asked winfo_ismapped() "
            "while the More options panel was closed")
    finally:
        window.cookie_file_var.set("")
        window.show_cookie_file()
        window.disclosure_var.set(False)
        window.toggle_extras()
        window.window.update_idletasks()


def test_skip_what_we_have_declining_keeps_the_file_and_drops_the_link(
        anydl_module, window, tmp_path, monkeypatch):
    video = tmp_path / "Old copy.mp4"
    video.write_bytes(b"x")
    anydl_module.LibraryIndex(str(tmp_path)).record(
        "https://example.com/watch/1",
        {"id": "1", "title": "Old copy", "height": 720},
        str(video), {"mode": "video"})

    monkeypatch.setattr(anydl_module.messagebox, "askyesno", lambda *a, **k: False)
    kept, replace = window.skip_what_we_have(
        ["https://example.com/watch/1"],
        {"out_dir": str(tmp_path), "mode": "video", "quality": "720p"})

    assert kept == []
    assert replace == set()


def test_skip_what_we_have_accepting_flags_replace_and_keeps_the_link(
        anydl_module, window, tmp_path, monkeypatch):
    video = tmp_path / "Old copy.mp4"
    video.write_bytes(b"x")
    anydl_module.LibraryIndex(str(tmp_path)).record(
        "https://example.com/watch/1",
        {"id": "1", "title": "Old copy", "height": 720},
        str(video), {"mode": "video"})

    monkeypatch.setattr(anydl_module.messagebox, "askyesno", lambda *a, **k: True)
    kept, replace = window.skip_what_we_have(
        ["https://example.com/watch/1"],
        {"out_dir": str(tmp_path), "mode": "video", "quality": "1080p"})

    assert kept == ["https://example.com/watch/1"]
    # A set of the links actually matched, not a bare flag: enqueue() uses
    # this to mark overwrites=True per URL, not for the whole pasted batch.
    assert replace == {"https://example.com/watch/1"}


def test_skip_what_we_have_is_silent_for_a_link_never_seen_before(
        anydl_module, window, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(anydl_module.messagebox, "askyesno",
                        lambda *a, **k: calls.append((a, k)) or True)

    kept, replace = window.skip_what_we_have(
        ["https://example.com/never-seen"], {"out_dir": str(tmp_path), "mode": "video"})

    assert kept == ["https://example.com/never-seen"]
    assert replace == set()
    assert not calls


def test_open_clip_editor_refuses_to_pass_an_inverted_range(anydl_module, window, monkeypatch):
    """clip_range() (used by Download) already refuses an end at or before its
    start. open_clip_editor() reads the same two boxes with its own, looser
    parsing and skipped that check, so a stale 'From 0:10 / To 0:05' pair
    opened the timeline with t_in > t_out -- which draws as the entire strip
    dimmed and no visible selection at all."""
    captured = {}

    class FakeEditor:
        def __init__(self, app, url, start, end):
            captured["start"] = start
            captured["end"] = end
            self.window = object()
            self.result = None

    # open_clip_editor lives in anydl_core.main_window and calls the
    # ClipEditor bound there; the re-export on `anydl` is a different
    # name and patching it would leave the real editor in place.
    from anydl_core import main_window

    monkeypatch.setattr(main_window, "ClipEditor", FakeEditor)
    monkeypatch.setattr(window.window, "wait_window", lambda *_a, **_k: None)

    window.urls_box.delete("1.0", "end")
    window.urls_box.insert("1.0", "https://example.com/watch?v=x")
    window.section_start_var.set("0:10")
    window.section_end_var.set("0:05")
    try:
        window.open_clip_editor()

        assert captured["start"] is None, captured
        assert captured["end"] is None, captured
    finally:
        window.urls_box.delete("1.0", "end")
        window.section_start_var.set("")
        window.section_end_var.set("")


def test_enqueue_only_marks_replace_for_the_url_that_was_actually_a_duplicate(
        anydl_module, window, tmp_path, monkeypatch):
    """skip_what_we_have asks one question for a whole pasted batch. Saying
    yes must not silently authorise overwriting a *different*, brand-new
    link that just happens to be queued in the same batch: two videos that
    sanitise to the same file name would otherwise have the second one
    silently overwrite the first because of a duplicate found elsewhere in
    the same paste."""
    video = tmp_path / "Old copy.mp4"
    video.write_bytes(b"x")
    anydl_module.LibraryIndex(str(tmp_path)).record(
        "https://example.com/watch/1",
        {"id": "1", "title": "Old copy", "height": 720},
        str(video), {"mode": "video"})

    monkeypatch.setattr(anydl_module.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(window, "ensure_worker", lambda: None)  # no real worker thread

    before = set(window.rows)
    window.enqueue(
        ["https://example.com/watch/1", "https://example.com/watch/2"],
        {"out_dir": str(tmp_path), "mode": "video", "quality": "1080p"})
    new_uids = sorted(set(window.rows) - before)
    try:
        assert len(new_uids) == 2, new_uids
        by_url = {window.rows[u].url: window.rows[u] for u in new_uids}
        assert by_url["https://example.com/watch/1"].settings.get("replace") is True
        assert not by_url["https://example.com/watch/2"].settings.get("replace"), (
            "a brand-new link queued alongside a duplicate was also marked "
            "replace=True, so it could silently overwrite another file")
    finally:
        for uid in new_uids:
            window.rows.pop(uid, None)
            if window.tree.exists(str(uid)):
                window.tree.delete(str(uid))
        with window.lock:
            window.items = [i for i in window.items if i.uid not in new_uids]
        window.show_empty_row()
        window.refresh_status()


class QuietSource:
    """A ClipSource that answers instantly and never touches the network.

    The editor is built for real -- the assertions are about widgets -- but
    the video is not: prepare() returns a length and no stills, and
    preview_frames() waits on a gate the test opens when it wants the
    abandoned decode to come back.
    """

    PLAY_FPS = 12
    GRID_WIDTH = 288

    def __init__(self, *_args, **_kwargs):
        self.duration = 20.0
        self.title = "A video"
        self.has_audio = False
        self.step = 0.5
        self.frames = []
        self.cancelled = False
        self.ffmpeg_dir = None
        self.gate = threading.Event()
        self.asked = 0

    def prepare(self, on_stage):
        on_stage("Fetching a small copy to scrub through...", 0.0)

    def frame_for(self, _seconds):
        return None

    def frame_time(self, seconds):
        return round(float(seconds) / self.step) * self.step

    def exact_frame(self, _seconds):
        return None

    def preview_frames(self, _start, _end):
        self.asked += 1
        self.gate.wait(10)  # a failing test must not hang the suite
        return ["never-read.png"], self.PLAY_FPS

    def play_audio(self, _start, _end):
        return False

    def stop(self):
        pass

    def close(self):
        self.cancelled = True


def build_editor(anydl_module, app, monkeypatch):
    """A ClipEditor on a QuietSource, built into a window nobody sees.

    Toplevel is swapped for one that hides itself, because a suite run should
    not flash a window; grab_set goes with it, since Tk refuses to grab a
    window that is not viewable and the editor calls it while building.
    """
    # Imported here rather than at the top of the file: collection has to work
    # on a machine without Tk, which is what importorskip above is for.
    import tkinter

    from anydl_core import trim_window

    source = QuietSource()

    class Hidden(tkinter.Toplevel):
        def __init__(self, *args, **kwargs):
            # super(), not tkinter.Toplevel.__init__: the name is about to be
            # this class, and calling through it recurses forever.
            super().__init__(*args, **kwargs)
            self.withdraw()

        def grab_set(self):
            pass

    monkeypatch.setattr(trim_window.tk, "Toplevel", Hidden)
    monkeypatch.setattr(trim_window, "ClipSource", lambda *a, **k: source)
    monkeypatch.setattr(trim_window, "fetch_transcript", lambda _url: [])

    editor = trim_window.ClipEditor(app, "https://example.com/watch", None, None)
    for _tick in range(80):
        if editor.duration > 0:
            break
        app.window.update()
        editor.window.update()
        time.sleep(0.02)
    return editor, source


def settle(app, editor, seconds=0.4):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.window.update()
        editor.window.update()
        time.sleep(0.02)


def test_stop_stops_while_the_clip_is_still_being_decoded(anydl_module, window,
                                                          monkeypatch):
    """The button reads "Stop" from the moment Play is pressed, and for the
    first stretch of that there is nothing to stop yet: ffmpeg is decoding the
    span into stills. Pressing it there used to do nothing at all -- play()
    returned early on `preparing` -- so on a long span the one control that
    said Stop was inert for several seconds. It now abandons the run, and the
    frames that arrive afterwards belong to a token nobody is waiting for."""
    editor, source = build_editor(anydl_module, window, monkeypatch)
    try:
        assert editor.duration > 0, "the fake source never reported a length"
        editor.t_in, editor.t_out = 1.0, 3.0

        editor.play()
        settle(window, editor, 0.2)
        assert editor.preparing is True
        assert editor.play_button.cget("text") == "Stop"
        assert source.asked == 1

        editor.play()  # the Stop press, while ffmpeg is still going
        assert editor.preparing is False
        assert editor.play_button.cget("text") == "Play"

        source.gate.set()  # the abandoned decode comes back now
        settle(window, editor, 0.4)
        assert editor.play_job is None, "playback started after it was stopped"
        assert editor.play_frames == []
        assert editor.play_button.cget("text") == "Play"
    finally:
        source.gate.set()
        editor.cancel()


def test_the_caption_only_warns_about_the_still_when_it_is_the_wrong_one(
        anydl_module, window, monkeypatch):
    """The frame under the playhead is the nearest one on the grid, which is
    worth saying while they differ and noise when they do not. The caption
    said it either way, so a playhead sitting exactly on a grid second read
    "0:03.0   nearest still 0:03" -- a warning about nothing."""
    editor, _source = build_editor(anydl_module, window, monkeypatch)
    try:
        editor.show_frame(3.0)
        on_grid = editor.frame_label.cget("text")
        editor.show_frame(3.2)
        off_grid = editor.frame_label.cget("text")

        assert on_grid == "0:03.0", on_grid
        assert off_grid == "0:03.2   nearest still 0:03", off_grid
    finally:
        editor.cancel()
