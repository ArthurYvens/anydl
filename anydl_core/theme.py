"""Colours, fonts and spacing resolved against the running platform."""

import sys
from tkinter import TclError, ttk
from tkinter import font as tkfont

from .config import DARK_PALETTE, EDITOR_PALETTE, LIGHT_PALETTE, SPACE


class Theme:
    """Colours, gaps and fonts, resolved once against the running platform.

    Two things here cannot be decided ahead of time. The surface colour is
    whatever the ttk theme supplies, so the text ramp has to be chosen after
    reading it. And every gap in the file is a pixel count while fonts are
    points, so on a scaled display the text grows and the spacing does not --
    px() puts them back on the same footing.
    """

    def __init__(self, window):
        self.style = ttk.Style(window)
        if sys.platform.startswith("linux") and "clam" in self.style.theme_names():
            # Tk's X11 default is the Motif-era theme, which is both dated and
            # the one theme of the three that cannot be styled at all.
            self.style.theme_use("clam")
        self.scale = max(1.0, window.winfo_fpixels("1i") / 96.0)
        self.surface = self.style.lookup("TFrame", "background") or "#f0f0f0"
        self.palette = DARK_PALETTE if self._is_dark(window) else LIGHT_PALETTE
        self._make_fonts(window)
        self._make_styles(window)

    # ------------------------------------------------------------- accessors
    def c(self, role):
        return self.palette[role]

    def px(self, name):
        return int(round(SPACE[name] * self.scale))

    def pad(self, *names):
        return tuple(self.px(name) for name in names)

    def scaled(self, pixels):
        """Any other pixel measurement, at the display's scale.

        px() only knows the names in SPACE, so wrap widths were being written
        as bare constants. At 150% the text grew and the wrap width did not,
        which turned every long label into extra lines.
        """
        return int(round(pixels * self.scale))

    def measure(self, name, text):
        """How wide that text is in one of the fonts this theme keeps alive.

        Sizing a column by hand is guesswork the moment the system font
        changes, and tkfont.Font(font=...) to measure with builds a second
        named font and deletes it again. The object is already here.
        """
        return self._fonts[name].measure(text)

    # --------------------------------------------------------------- private
    def _is_dark(self, window):
        try:
            red, green, blue = window.winfo_rgb(self.surface)
        except Exception:  # noqa: BLE001
            return False
        return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 65535.0 < 0.5

    def _make_fonts(self, window):
        base = tkfont.nametofont("TkDefaultFont")
        size = base.cget("size") or 9
        family = base.cget("family")
        # A Tk size is points when positive and pixels when negative, and some
        # systems report the second kind. A fixed +1 would shrink the text
        # there and invert the whole scale, so the deltas follow the sign.
        step = 1 if size > 0 else -1

        # Every Font object has to be kept alive. tkinter.font.Font.__del__
        # runs "font delete", so letting one fall out of scope destroys the
        # named font moments after it is created. The styles below then name a
        # font Tk no longer has; Tk reads the name as a font *family* instead,
        # fails to find that too, and silently falls back to Arial 12 for every
        # styled widget in the app -- which is what it did until this dict.
        self._fonts = {}

        def make(name, delta=0, weight="normal", face=None):
            try:
                font = tkfont.Font(window, name=name, exists=False,
                                   family=face or family,
                                   size=size + delta * step, weight=weight)
            except Exception:  # noqa: BLE001
                font = tkfont.nametofont(name)
                font.configure(family=face or family,
                               size=size + delta * step, weight=weight)
            self._fonts[name] = font
            return name

        self.title = make("AnydlTitle", 4, "bold")
        self.section = make("AnydlSection", 0, "bold")
        self.body = make("AnydlBody", 0)
        self.action = make("AnydlAction", 1, "bold")
        # One step down is the floor: below this Segoe UI stops being readable
        # at 100% scaling.
        self.small = make("AnydlSmall", -1)
        # TkFixedFont is pinned to Courier New 10 on Windows, so leaning on it
        # would make the queue the one region that ignores the OS text size.
        self.mono = make("AnydlMono", 1, face=tkfont.nametofont("TkFixedFont").cget("family"))
        self.mono_size = size + step
        # The editor's clock, read from across the desk like a monitor's.
        self.timecode = make("AnydlTimecode", 6, face=tkfont.nametofont("TkFixedFont").cget("family"))

    def _make_styles(self, window):
        style, colour = self.style, self.c
        # ttk's native height for these is 21px, under the 24px minimum for a
        # pointer target -- and four preset radios sit close together.
        for control in ("TCheckbutton", "TRadiobutton"):
            style.configure(control, padding=(0, self.px("xs")))
        style.configure("TLabel", font=self.body)
        style.configure("Muted.TLabel", font=self.small, foreground=colour("muted"))
        # The label of a control that does not apply right now. It differs
        # from Muted only in keeping the body font: greying these with the
        # small font as well changed their size -- "Audio format:" measures
        # 78x19 in one and 75x17 in the other -- so switching between video
        # and audio nudged every control on that row sideways and re-centred
        # the row around them.
        style.configure("Off.TLabel", font=self.body, foreground=colour("muted"))
        # A caption that has to report a failure without resizing its row. ttk
        # resolves a dotted name by dropping leading words, so this one takes
        # Muted's small font and changes only the colour: measured AnydlSmall
        # in both, so the line stays 17px tall whether it is saying nothing or
        # saying ffmpeg could not decode the span. The colour is the palette's
        # warning, 5.90:1 on the Windows surface and 4.81:1 on clam's.
        style.configure("Warning.Muted.TLabel", foreground=colour("warning"))
        style.configure("Section.TLabel", font=self.section)
        style.configure("Title.TLabel", font=self.title, foreground=colour("accent"))
        style.configure("Head.TLabel", font=self.title)
        style.configure("Error.TLabel", font=self.body, foreground=colour("error"))
        style.configure("Warning.TLabel", font=self.body, foreground=colour("warning"))
        style.configure("Success.TLabel", font=self.body, foreground=colour("success"))
        style.configure("Banner.TFrame", background=colour("warn_bg"))
        style.configure("Banner.TLabel", background=colour("warn_bg"),
                        foreground=colour("on_warn"), font=self.body)
        # -background on a button is discarded by the Windows and macOS theme
        # engines, so the primary action has to earn its weight from type size
        # and padding instead of from a fill.
        style.configure("Primary.TButton", font=self.action,
                        padding=(self.px("lg"), self.px("sm")))
        # The small font is a step below the body text, which on its own
        # leaves this button about 20px tall -- under the 24px minimum for a
        # pointer target. The padding buys the height back.
        style.configure("Link.TButton", font=self.small,
                        padding=(self.px("sm"), self.px("xs")))
        # The options column. ttk resolves "Side.Muted.TLabel" by dropping the
        # leading word, so each keeps its parent's font and colour and only
        # the background changes.
        for name in ("TFrame", "TLabel", "Muted.TLabel", "Off.TLabel", "Section.TLabel",
                     "Title.TLabel", "Head.TLabel", "TRadiobutton", "TCheckbutton",
                     "TButton", "Primary.TButton", "Link.TButton", "Toolbutton"):
            style.configure("Side." + name, background=colour("side"))
        self._make_editor_styles()
        style.configure("Treeview.Heading", font=self.section)
        # Measured on the kept font object: building a throwaway Font here
        # would create, and then delete, another named font. The gap is "sm"
        # rather than "xs" because a row is a pointer target -- it selects, it
        # opens a folder on a double click, it carries the right-click menu --
        # and 16px of line plus 4px left it 20px tall, under the 24px SC 2.5.8
        # asks for. 16 + 8 is 24 exactly, and the text stops looking crammed.
        style.configure("Queue.Treeview", font=self.mono,
                        rowheight=self._fonts[self.mono].metrics("linespace")
                        + self.px("sm"))
        # The same table holding sentences instead of fields. Mono is what
        # keeps a column of sizes and percentages lined up; a line of speech
        # gets no such favour from it, only about a fifth fewer words on the
        # row. Body linespace is 15px here, so the same "sm" gap that made the
        # queue exactly 24 leaves this one at 23: the floor is explicit rather
        # than arithmetic, because a caption row is a pointer target too --
        # one click moves the playhead, two cut the clip to the line.
        style.configure("Transcript.Treeview", font=self.body,
                        rowheight=max(self._fonts[self.body].metrics("linespace")
                                      + self.px("sm"), self.scaled(24)))
        # A tag foreground drawn over the selection blue is unreadable, so the
        # theme's own selected pair has to stay in the map.
        selected = self._selected_foreground()
        keep = [entry for entry in style.map("Treeview", "foreground")
                if "selected" not in str(entry[0])]
        style.map("Treeview", foreground=keep + [("selected", selected)])
        # The combobox dropdown is a classic Tk listbox and is not themed; left
        # alone it stays grey while everything around it changes.
        window.option_add("*TCombobox*Listbox.font", self.body)

    def _make_editor_styles(self):
        """The trim window's dark family, on every platform.

        Labels, frames and the transcript take a background on any ttk
        theme. Scrollbars do not -- vista draws them natively -- so theirs are
        built from clam's elements, which take colours wherever they run.
        """
        style, tone = self.style, EDITOR_PALETTE
        for name, fg in (("TFrame", None), ("TLabel", "text"), ("Muted.TLabel", "muted"),
                         ("Warning.Muted.TLabel", "warning"), ("Section.TLabel", "text"),
                         ("Head.TLabel", "text")):
            style.configure("Editor." + name, background=tone["bg"],
                            **({"foreground": tone[fg]} if fg else {}))
        style.configure("Editor.Timecode.TLabel", background=tone["bg"],
                        foreground=tone["text"], font=self.timecode)
        # vista draws a Treeview's field natively, white whatever it is told,
        # so the field comes from clam as well.
        try:
            style.element_create("Editor.Treeview.field", "from", "clam", "field")
        except TclError:
            pass
        style.layout("Editor.Transcript.Treeview", [
            ("Editor.Treeview.field", {"sticky": "nswe", "border": "1", "children": [
                ("Treeview.padding", {"sticky": "nswe", "children": [
                    ("Treeview.treearea", {"sticky": "nswe"})]})]})])
        style.configure("Editor.Transcript.Treeview", background=tone["field"],
                        fieldbackground=tone["field"], foreground=tone["text"],
                        bordercolor=tone["line"], lightcolor=tone["field"],
                        darkcolor=tone["field"])
        style.map("Editor.Transcript.Treeview",
                  background=[("selected", tone["accent"])],
                  foreground=[("selected", tone["on_accent"])])
        for orient in ("Vertical", "Horizontal"):
            parts = ["Editor.%s.Scrollbar.%s" % (orient, part) for part in ("trough", "thumb")]
            for name, source in zip(parts, ("trough", "thumb")):
                try:
                    style.element_create(name, "from", "clam", source)
                except TclError:
                    pass  # a second window in the same interpreter
            style.layout("Editor.%s.TScrollbar" % orient, [
                (parts[0], {"sticky": "nswe", "children": [
                    (parts[1], {"expand": "1", "sticky": "nswe"})]})])
            style.configure("Editor.%s.TScrollbar" % orient, troughcolor=tone["field"],
                            background=tone["raised"], bordercolor=tone["field"],
                            lightcolor=tone["raised"], darkcolor=tone["raised"],
                            gripcount=0, arrowsize=self.scaled(10))
            style.map("Editor.%s.TScrollbar" % orient,
                      background=[("active", tone["raised_hover"])])

    def _selected_foreground(self):
        for entry in self.style.map("Treeview", "foreground"):
            if "selected" in str(entry[0]):
                return entry[1]
        return "SystemHighlightText" if sys.platform == "win32" else "#ffffff"
