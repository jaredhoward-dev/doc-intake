"""pytest fixtures: a throwaway records root behind the fake host (tests/host.py), and no real dialog ever."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host  # noqa: E402,F401  configures the engine for every test


@pytest.fixture
def records(tmp_path, monkeypatch):
    """A throwaway records root. Nothing outside tmp_path is read or written."""
    root = tmp_path / "Records"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("DOC_INTAKE_TEST_ROOT", str(root))
    monkeypatch.delenv("DOC_INTAKE_MODEL", raising=False)
    host.configure()
    return root


@pytest.fixture(autouse=True)
def no_real_dialog(monkeypatch):
    """No test ever shows a dialog on the desktop: the user's click is stood in for by an accept, unless a test
    (tests/test_approval.py) puts the real function back. No display either, so a leak fails closed."""
    from doc_intake import approval
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(approval, "ask_desktop", lambda title, text, run=None: True)
    monkeypatch.setattr(approval, "tty_io", lambda: (_ for _ in ()).throw(approval.ApprovalError("no tty in tests")))
