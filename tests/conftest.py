import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def anydl_module():
    """Load the repository's anydl.py without requiring package installation."""
    path = Path(__file__).resolve().parents[1] / "anydl.py"
    if not path.exists():
        pytest.skip("anydl.py is not present in this checkout")

    spec = importlib.util.spec_from_file_location("anydl", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["anydl"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def design_probe():
    """Load scripts/design_probe.py, the way anydl.py is loaded above.

    The contrast arithmetic and the table of surfaces live in the probe. The
    tests import them rather than restating them, so a surface added to the
    report is a surface the suite starts guarding on its own.
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / "design_probe.py"
    if not path.exists():
        pytest.skip("scripts/design_probe.py is not present in this checkout")

    spec = importlib.util.spec_from_file_location("design_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["design_probe"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def converter(anydl_module, monkeypatch):
    """A Converter that believes ffmpeg is not installed.

    The patch has to land on the module that looks the name up, not on
    `anydl`. Converter lives in anydl_core.converter and resolves
    find_ffmpeg in its own namespace; patching the re-export on `anydl`
    changes a name nothing reads, and the fixture would quietly hand back a
    converter that can see the real ffmpeg.
    """
    from anydl_core import converter as converter_module

    monkeypatch.setattr(converter_module, "find_ffmpeg", lambda: None)
    return anydl_module.Converter(on_log=lambda *_: None)
