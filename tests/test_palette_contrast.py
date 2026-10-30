"""The palettes, held to their minimums without needing a display.

`scripts/design_probe.py` is the report a person reads; this is the guard that
runs whether anyone remembers to read it or not. It is deliberately arithmetic
over the nominal backgrounds instead of a call into Tk, because the machine
that runs the suite has no display -- and a guard that skips exactly where
nobody is watching is not a guard. The probe is imported rather than copied, so
the numbers here and the numbers it prints cannot drift apart.

The one thing this cannot do is discover a surface: it checks the backgrounds
the probe already knows about. A ttk theme that starts handing out something
else is caught by running the probe on that platform, not by this file.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def ratio(probe, front, back):
    first, second = probe.luminance_of(front), probe.luminance_of(back)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def shortfalls(probe, palette, surface):
    """Every pair in the palette that misses its minimum on this surface."""
    missed = []
    for role, against, minimum, what in probe.CHECKS:
        background = surface if against == "surface" else palette[against]
        measured = ratio(probe, palette[role], background)
        # The probe prints two decimals, so a pair that reads as 4.50 there has
        # to pass here; without the slack a 4.499 would fail a test whose own
        # report says it passed.
        if measured + 0.005 < minimum:
            missed.append("%s on %s (%s) is %.2f:1, wants %.1f:1 -- %s"
                          % (role, against, background, measured, minimum, what))
    return missed


def test_light_palette_clears_every_surface_it_can_be_handed(anydl_module,
                                                             design_probe):
    """A colour has to work on all three, not just on the one you develop on.

    This is the regression the suite exists for: the "Done" green passed on
    Windows at 4.46:1 -- already short -- while measuring 3.64:1 on the surface
    clam hands out on Linux, and nothing running on Windows could tell.
    """
    missed = []
    for theme, surface in design_probe.SURFACES:
        for line in shortfalls(design_probe, anydl_module.LIGHT_PALETTE, surface):
            missed.append("%s %s: %s" % (theme, surface, line))

    assert not missed, "\n".join(missed)


def test_dark_palette_clears_its_assumed_surface(anydl_module, design_probe):
    missed = shortfalls(design_probe, anydl_module.DARK_PALETTE,
                        design_probe.DARK_SURFACE)

    assert not missed, "\n".join(missed)


def test_dark_border_holds_across_the_whole_band(anydl_module, design_probe):
    """The dark surface is a guess, so the border is sized for a range.

    The comment on DARK_PALETTE claims #999999 keeps 3:1 anywhere from #1e1e1e
    to #4c4c4c. That claim is the only thing standing between the guess and an
    invisible field edge on a Mac nobody here can test on, so it is checked
    rather than believed.
    """
    border = anydl_module.DARK_PALETTE["border"]
    thin = [(edge, ratio(design_probe, border, edge))
            for edge in design_probe.DARK_BAND]

    assert all(measured + 0.005 >= 3.0 for _, measured in thin), \
        "; ".join("%s: %.2f:1" % pair for pair in thin)


def test_light_and_dark_disagree_about_every_colour_they_should(anydl_module):
    """DARK_PALETTE is built from LIGHT_PALETTE, so a new light role is dark by
    default -- silently, and with a light-surface value. Anything readable has
    to be restated; only the two roles that carry their own background may be
    shared."""
    light, dark = anydl_module.LIGHT_PALETTE, anydl_module.DARK_PALETTE
    shared = {role for role in light if light[role] == dark.get(role)}

    assert shared == {"sunken", "on_sunken"}, sorted(shared)


def test_probe_itself_reports_clean():
    """The report has to keep running, not just the arithmetic under it.

    Skipped where there is no display: it is the sweep that needs Tk, and the
    tests above are what hold the line on a headless machine.
    """
    tkinter = pytest.importorskip("tkinter")
    try:
        window = tkinter.Tk()
    except Exception as problem:  # noqa: BLE001 -- any Tcl failure means no display
        pytest.skip("no display for Tk: %s" % problem)
    window.withdraw()  # else a bare root flashes on screen mid-suite
    window.destroy()

    probe = ROOT / "scripts" / "design_probe.py"
    if not probe.exists():
        pytest.skip("scripts/design_probe.py is not present in this checkout")

    done = subprocess.run([sys.executable, str(probe), "--strict"],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=120)

    assert done.returncode == 0, done.stdout + done.stderr
