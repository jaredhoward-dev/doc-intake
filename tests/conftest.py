"""pytest fixtures: a throwaway records root behind the fake host (tests/host.py), and no real dialog ever."""
import os
import resource
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host  # noqa: E402  configures the engine for every test
from doc_intake import reader  # noqa: E402


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


@pytest.fixture(autouse=True)
def macos_rlimit(monkeypatch):
    """macOS refuses RLIMIT_AS (setrlimit raises ValueError), so reader.run_bounded's child dies and every bounded
    read comes back "too large" (docs/followups.md). On macOS only, skip that one limit so the tests exercise the
    rest; Linux (CI) runs the real bound, and the tests that assert the cap bites are skipped elsewhere."""
    if sys.platform != "darwin":
        return
    real = resource.setrlimit

    def setrlimit(which, limits):
        try:
            real(which, limits)
        except ValueError:
            if which != resource.RLIMIT_AS:
                raise
    monkeypatch.setattr(reader.resource, "setrlimit", setrlimit)


@pytest.fixture(autouse=True)
def fresh_config():
    """Every test starts from the fake host's configuration: a hook one test sets never reaches the next."""
    host.configure()
    yield
    host.configure()
