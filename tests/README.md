# AnyDL test suite

These tests are deliberately offline.

They exercise pure helpers and the `Converter` option/hook logic without
contacting websites, downloading media, reading cookies, or modifying the
installed Python environment.

## Setup

From the repository root:

```bash
python -m venv .venv
# activate it
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e ".[dev]"
```

If you do not want editable installation, installing pytest and ruff is enough:

```bash
python -m pip install "pytest>=8,<9" "ruff>=0.6,<1"
```

## Run

```bash
python -m compileall .
python -m pytest -q
python -m ruff check .
```

The tests expect the real repository `anydl.py` to be present at the project
root. Copy this `tests/` directory, `scripts/`, `.claude/`, `CLAUDE.md`, and
`pyproject.toml` into your AnyDL checkout. Without `scripts/` the contrast
tests skip rather than fail, so a checkout missing it looks green.

## The contrast guard

`test_palette_contrast.py` holds every palette colour to its WCAG minimum on
every window background a ttk theme can hand the application -- `#f0f0f0` on
Windows, `#dcdad5` on Linux, a nominal `#ececec` for macOS aqua -- plus the
band the dark border was sized for.

It is arithmetic, not a screenshot, and it needs no display: the surfaces and
the luminance function are imported from `scripts/design_probe.py` so the
guard and the report cannot disagree. This matters because the colour that
prompted it passed on Windows and failed on Linux, and nothing running on
Windows could see that.

For the same numbers as a readable report, with the measured surface of each
theme installed on the machine:

```bash
python scripts/design_probe.py
```

Only the last test in the file needs Tk; it runs the probe end to end and
skips where there is no display.
