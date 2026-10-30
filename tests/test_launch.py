"""The GUI launch path, checked without opening a window.

`python anydl.py` with no URL ends in `launch_gui()`, and nothing in the
suite used to call it: the window tests build `AnydlApp` themselves after
preparing Tk by hand, so every line of launch_gui except the constructor was
unexercised. It spent that time calling `_load_tk()`, which stayed behind in
anydl.py when the code was split into anydl_core, and raised NameError before
the first window existed -- a green suite on top of an application that could
not start.

These need no display: `AnydlApp` is replaced, so no Tk interpreter is made.
"""

import pytest


@pytest.fixture
def main_window(anydl_module):
    """The window module, imported the way the conftest imports converter.

    Loading anydl.py is what puts the project root on sys.path, so the import
    has to happen after that fixture has run, not at module import time.
    """
    pytest.importorskip("tkinter")
    from anydl_core import main_window as module

    return module


def test_launch_gui_runs_the_app(main_window, monkeypatch):
    """The whole path runs: no NameError, and the window actually gets run."""
    calls = []

    class FakeApp:
        def __init__(self):
            calls.append("built")

        def run(self):
            calls.append("ran")

    monkeypatch.setattr(main_window, "claim_taskbar_identity",
                        lambda: calls.append("taskbar"))
    monkeypatch.setattr(main_window, "AnydlApp", FakeApp)

    main_window.launch_gui()

    assert calls == ["taskbar", "built", "ran"]


def test_launch_gui_claims_the_taskbar_before_building(main_window, monkeypatch):
    """Order matters: the identity has to be claimed while no window exists.

    Windows reads it when the first window is created, so a claim made after
    the constructor is a claim that changes nothing and an icon that reverts
    to the generic Python one in the taskbar.
    """
    seen = []

    class FakeApp:
        def __init__(self):
            seen.append("built")

        def run(self):
            pass

    monkeypatch.setattr(main_window, "claim_taskbar_identity",
                        lambda: seen.append("taskbar"))
    monkeypatch.setattr(main_window, "AnydlApp", FakeApp)

    main_window.launch_gui()

    assert seen.index("taskbar") < seen.index("built")


def test_cli_with_no_url_opens_the_window(anydl_module, monkeypatch):
    """`python anydl.py` with no arguments reaches launch_gui and nothing else.

    The import inside cli.main() is deliberate -- the command line has to work
    on a machine with no tkinter -- which also means a typo in it would only
    ever show up at runtime, in front of a user.
    """
    pytest.importorskip("tkinter")
    from anydl_core import cli, main_window

    opened = []
    monkeypatch.setattr(main_window, "launch_gui", lambda: opened.append(True))
    monkeypatch.setattr(cli.sys, "argv", ["anydl"])

    cli.main()

    assert opened == [True]
