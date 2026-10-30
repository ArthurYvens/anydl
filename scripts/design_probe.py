"""Measure the design tokens as the running platform actually resolves them.

The palettes in anydl.py carry contrast ratios in their comments, and those
numbers only mean anything if something recomputes them. A ttk surface is a
symbolic name ("SystemButtonFace"), the fonts come from the OS, and a gap is
scaled by the display -- none of it can be checked by reading the source.

    python scripts/design_probe.py            # report
    python scripts/design_probe.py --strict   # exit 1 if any pair fails

One surface is not enough. Theme forces clam on Linux and accepts the platform
default elsewhere, so the same token is drawn on clam's #dcdad5, on Windows'
#f0f0f0 and on aqua's light grey -- and the darkest of those is where text
fails first. Measuring only the surface this machine happens to hand out lets a
colour pass here and fail on another operating system, which is exactly how the
success green sat below the minimum on Linux while passing on Windows. So the
light palette is measured against every surface the application can be given.

No machine has all of them: aqua only exists on macOS, vista only on Windows.
A theme that is not installed here is measured against its nominal background
and marked assumed in the report.

The dark palette is only reachable where the ttk theme follows the OS, which
today means macOS aqua. Off that platform its surface has to be assumed, so it
is reported against --dark-surface (a nominal aqua dark window) and marked.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# role -> (background role, minimum ratio, what the pair is)
CHECKS = (
    ("muted", "surface", 4.5, "secondary text on the window"),
    ("accent", "surface", 4.5, "title and link text"),
    ("success", "surface", 4.5, "success text"),
    ("warning", "surface", 4.5, "warning text"),
    ("error", "surface", 4.5, "error text"),
    ("border", "surface", 3.0, "field outline against the window"),
    ("muted", "field", 4.5, "queue row text"),
    ("accent", "field", 4.5, "queue row text"),
    ("success", "field", 4.5, "queue row text"),
    ("warning", "field", 4.5, "queue row text"),
    ("error", "field", 4.5, "queue row text"),
    ("border", "field", 3.0, "field outline against the field"),
    ("muted", "side", 4.5, "secondary text in the options column"),
    ("accent", "side", 4.5, "title in the options column"),
    ("warning", "side", 4.5, "warning text in the options column"),
    ("error", "side", 4.5, "error text in the options column"),
    ("border", "side", 3.0, "field outline in the options column"),
    ("on_warn", "warn_bg", 4.5, "banner text"),
    ("on_sunken", "sunken", 4.5, "log text"),
)

# The ttk themes AnyDL can actually be handed, with the background to fall back
# on where the theme is not installed. Theme forces clam on Linux and takes the
# platform default on Windows and macOS, so alt, default and classic never
# reach a user: sweeping them would report failures against a surface the
# application never has, and the noise would hide the ones that matter.
AQUA_SURFACE = "#ececec"  # nominal macOS light window
SURFACES = (
    ("vista", "#f0f0f0"),
    ("winnative", "#f0f0f0"),
    ("clam", "#dcdad5"),
    ("aqua", AQUA_SURFACE),
)

# The dark palette has no theme here to ask, so its window is nominal too, and
# the border over it was picked to hold across a band rather than at one value.
# The band is the claim the comment on DARK_PALETTE makes; naming it here lets
# a test hold that claim to account instead of trusting the prose.
DARK_SURFACE = "#323232"
DARK_BAND = ("#1e1e1e", "#4c4c4c")


def load_anydl():
    path = ROOT / "anydl.py"
    spec = importlib.util.spec_from_file_location("anydl", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["anydl"] = module
    spec.loader.exec_module(module)
    return module


def _channel(value):
    value /= 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _weigh(red, green, blue):
    return (0.2126 * _channel(red) + 0.7152 * _channel(green)
            + 0.0722 * _channel(blue))


def luminance_of(colour):
    """Relative luminance of a "#rrggbb" string, with no display involved.

    The sweep needs a window to turn a symbolic name like SystemButtonFace into
    channels, but every colour in the palettes is already hex. The test suite
    runs where there is no display, so it has to reach this arithmetic without
    Tk -- and it has to be *this* arithmetic, not a second copy of it, or the
    guard and the report can disagree about the same pair.
    """
    text = colour.lstrip("#")
    return _weigh(*(int(text[at:at + 2], 16) for at in (0, 2, 4)))


def make_luminance(window):
    def luminance(colour):
        return _weigh(*(part / 257.0 for part in window.winfo_rgb(colour)))
    return luminance


def ratio(luminance, front, back):
    first, second = luminance(front), luminance(back)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def resolve(window, colour):
    red, green, blue = (part // 257 for part in window.winfo_rgb(colour))
    return "#%02x%02x%02x" % (red, green, blue)


def sweep_surfaces(window, style, aqua_surface):
    """Every window background the application can be handed.

    Asked of the theme where the theme exists on this machine, and taken from
    the nominal value where it does not. The theme in use is put back before
    returning, because the rest of the report describes the window the user of
    this machine would actually get.
    """
    original = style.theme_use()
    installed = set(style.theme_names())
    missing = "assumed: theme not installed here"
    found = []
    for name, nominal in SURFACES:
        if name == "aqua":
            nominal = aqua_surface
        surface, note = nominal, missing
        if name in installed:
            try:
                style.theme_use(name)
                surface = resolve(window,
                                  style.lookup("TFrame", "background") or nominal)
                note = ""
            except Exception:  # noqa: BLE001
                surface, note = nominal, missing
        found.append((name, surface, note))
    style.theme_use(original)

    # vista and winnative hand out the same SystemButtonFace, and printing the
    # same six rows twice would bury the surface that differs.
    merged = []
    for name, surface, note in found:
        if merged and merged[-1][1] == surface and merged[-1][2] == note:
            merged[-1][0].append(name)
        else:
            merged.append(([name], surface, note))
    return [(", ".join(names), surface, note) for names, surface, note in merged]


def report_pair(luminance, front, background, role, against, minimum, what):
    measured = ratio(luminance, front, background)
    ok = measured + 0.005 >= minimum
    print("  %-4s %-10s on %-8s %5.2f:1  (needs %.1f)  %s"
          % ("ok" if ok else "FAIL", role, against, measured, minimum, what))
    return measured, ok


def report_palette(name, palette, surfaces, luminance):
    """Print one block for the pairs that do not depend on the window, then
    one block per surface the window can have. A failure carries the surface
    it happened on: the ratio alone does not say where to go and fix it."""
    print("\n%s palette" % name)
    failures = []
    for role, against, minimum, what in CHECKS:
        if against == "surface":
            continue
        measured, ok = report_pair(luminance, palette[role], palette[against],
                                   role, against, minimum, what)
        if not ok:
            failures.append((role, against, measured, minimum, what))

    for label, surface, note in surfaces:
        print("\n  %-18s %s  (%s)" % (label, surface, note or "measured"))
        for role, against, minimum, what in CHECKS:
            if against != "surface":
                continue
            measured, ok = report_pair(luminance, palette[role], surface,
                                       role, against, minimum, what)
            if not ok:
                failures.append((role, "%s %s" % (label, surface),
                                 measured, minimum, what))
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when a pair is below its minimum")
    parser.add_argument("--dark-surface", default=DARK_SURFACE,
                        help="assumed window background for the dark palette")
    parser.add_argument("--aqua-surface", default=AQUA_SURFACE,
                        help="assumed light window background for macOS aqua")
    args = parser.parse_args()

    anydl = load_anydl()
    anydl._load_tk()
    window = anydl.tk.Tk()
    window.withdraw()
    theme = anydl.Theme(window)
    luminance = make_luminance(window)
    surfaces = sweep_surfaces(window, theme.style, args.aqua_surface)

    print("ttk theme   %s" % theme.style.theme_use())
    print("platform    %s" % sys.platform)
    print("surface     %s -> %s" % (theme.surface, resolve(window, theme.surface)))
    print("scale       %.2fx" % theme.scale)
    print("dark        %s" % (theme.palette is anydl.DARK_PALETTE))

    print("\nspacing")
    for token in anydl.SPACE:
        print("  %-3s %3dpx" % (token, theme.px(token)))

    print("\nfonts")
    for name in sorted(theme._fonts):
        font = theme._fonts[name]
        actual = font.actual()
        print("  %-12s %-20s %3s %-6s  linespace %dpx"
              % (name, actual["family"], actual["size"], actual["weight"],
                 font.metrics("linespace")))

    failures = report_palette("light", anydl.LIGHT_PALETTE, surfaces, luminance)
    # The dark palette keeps the single assumed surface it always had. It is
    # only reachable where the ttk theme follows the OS, so there is no second
    # theme to sweep, and the band of dark greys it has to survive is argued
    # beside the token in anydl.py rather than enumerated here.
    dark_note = "nominal --dark-surface"
    if theme.palette is not anydl.DARK_PALETTE:
        dark_note = "assumed: the dark palette is not reachable here"
    failures += report_palette("dark", anydl.DARK_PALETTE,
                               [("aqua dark", args.dark_surface, dark_note)],
                               luminance)

    window.destroy()

    print("\n%d pair(s) below the minimum" % len(failures))
    for role, against, measured, minimum, what in failures:
        print("  %s on %s is %.2f:1, wants %.1f:1 -- %s"
              % (role, against, measured, minimum, what))
    if failures and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
