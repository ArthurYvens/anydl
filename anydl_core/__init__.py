"""anydl, split into modules.

Nothing is imported here on purpose. `cli` must be usable on a machine with
no tkinter installed, and importing the window modules from this file would
pull it in the moment anyone touched the package. Import what you need:

    from anydl_core.converter import Converter
    from anydl_core.cli import main

`anydl.py` at the project root is the entry point and re-exports the names
the test suite reaches for.
"""
