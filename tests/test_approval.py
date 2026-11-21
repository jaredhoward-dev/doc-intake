"""Approval integrity (a hash over the approved content) and the user's confirmation (a desktop dialog).

Through the fake host (tests/host.py) against a throwaway records root. No real dialog and no real terminal: the
dialog, the phrase and /dev/tty are stood in for by fakes. Every document here is made up."""
import contextlib
import json
import subprocess

import pytest

from doc_intake import approval, places, tasks
from helpers import ACCOUNT, ROUTING, SSN, drop, run
import host

REAL_ASK = approval.ask_desktop  # the conftest fixture replaces it after import
REAL_TTY_IO = approval.tty_io  # the same

PAYSTUB = f"""Example Payroll Co   Earnings Statement
Pay date: 01/15/2030    Pay period: 01/01/2030 - 01/14/2030
Employee ID 00123456    SSN {SSN}
Earnings        Rate     Hours    Current      YTD
Regular         25.00    80.00    2,000.00     2,000.00
Gross Pay                         2,000.00     2,000.00
Federal Income Tax                  200.00       200.00
State Income Tax                     81.00        81.00
Social Security                     124.00       124.00
Medicare                             29.00        29.00
401(k)                              100.00       100.00
Medical                              45.00        45.00
Roth 401(k)                          50.00        50.00
Net Pay                           1,372.00     1,372.00
Direct deposit  Routing {ROUTING}  Account {ACCOUNT}   1,372.00
"""


def _task(rec):
    drop(rec, "stub.txt", PAYSTUB)
    assert run(["fields", "--task", "paystub", "inbox/stub.txt"]) == 0
    (t,) = (rec / host.STATE / "tasks").iterdir()
    return t


def approved_task(rec):
    t = _task(rec)
    assert run(["approve", t.name]) == 0
    return t


def edit(path, fn):
    t = json.loads(path.read_text())
    fn(t)
    path.write_text(json.dumps(t, indent=1))


# ---------------------------------------------------------------- integrity
def test_approve_stores_hash_and_time(rec):
    t = approved_task(rec)
    body = json.loads(t.read_text())
    assert body["approved"] is True and len(body["approved_hash"]) == 64 and body["approved_at"][:4].isdigit()
    assert body["approved_hash"] == approval.task_digest(body)


@pytest.mark.parametrize("tamper", [
    lambda t: t["values"][0].update(value="9,999.00"),                       # a value changed
    lambda t: t["values"].append({**t["values"][0], "field": "extra"}),      # a field added
    lambda t: t["sources"][0].update(sha256="0" * 64),                       # a source hash rewritten
    lambda t: t["dropped"].clear() or t["dropped"].update(ssn=0),            # anything else in the content
], ids=["value", "field-added", "source-hash", "other-content"])
def test_tampered_task_is_refused(rec, capsys, tamper):
    t = approved_task(rec)
    edit(t, tamper)
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "r"]) == 2
    assert "changed since approval; re-run fields and approve again" in capsys.readouterr().err


def test_flag_alone_without_hash_is_refused(rec, capsys):
    t = _task(rec)
    edit(t, lambda b: b.update(approved=True))
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "r"]) == 2
    assert "without an integrity hash" in capsys.readouterr().err


def test_keep_and_supersede_do_not_break_the_hash(rec, capsys):
    t = approved_task(rec)
    assert run(["keep", t.name]) == 0
    assert run(["read", t.name, "--reason", "r"]) == 0
    assert run(["fields", "--task", "paystub", "inbox/stub.txt", "--replaces", t.name]) == 0
    new = t.with_name(t.name[:-5] + "-v2.json")
    assert json.loads(t.read_text())["superseded_by"] == new.name
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "r"]) == 2 and "superseded" in capsys.readouterr().err
    assert run(["approve", new.name]) == 0
    assert run(["read", new.name, "--reason", "r"]) == 0


def test_changed_source_document_is_refused(rec, capsys):
    t = approved_task(rec)
    (rec / host.INBOX / "stub.txt").write_text(PAYSTUB.replace("Example Payroll", "Other Payroll"))
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "r"]) == 2
    assert "source document inbox/stub.txt changed since fields ran" in capsys.readouterr().err


def test_missing_source_document_is_refused(rec, capsys):
    t = approved_task(rec)
    (rec / host.INBOX / "stub.txt").unlink()
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "r"]) == 2
    assert "is missing" in capsys.readouterr().err


def test_moved_source_is_found_through_the_index(rec):
    t = approved_task(rec)
    src = rec / host.INBOX / "stub.txt"
    dest = rec / "Pay" / "2030" / "stub.txt"
    dest.parent.mkdir(parents=True)
    digest = places.sha256(str(src))
    src.rename(dest)
    idx = places.load_index()
    idx.setdefault("files", {})[digest] = {"status": "filed", "path": "Pay/2030/stub.txt"}
    places.save_index(idx)
    assert run(["read", t.name, "--reason", "r"]) == 0


def test_approved_task_for_ignores_a_tampered_receipt(rec):
    drop(rec, "r.txt", "ACME Hardware\n2030-03-01\nTOTAL $12.34\nVISA ****1111\n")
    run(["fields", "--task", "receipt", "inbox/r.txt"])
    (t,) = (rec / host.STATE / "tasks").iterdir()
    run(["approve", t.name])
    digest = json.loads(t.read_text())["sources"][0]["sha256"]
    assert tasks.approved_task_for(digest)
    edit(t, lambda b: b["values"].append({**b["values"][0], "field": "tax"}))
    assert tasks.approved_task_for(digest) is None


def _extract(rec):
    drop(rec, "multi.txt", f"page one SSN {SSN}\fpage two")
    assert run(["extract", "inbox/multi.txt", "--pages", "1"]) == 0
    (ex,) = (rec / host.STATE / "extracts").iterdir()
    return ex


def test_extract_hash_and_tamper(rec, capsys):
    ex = _extract(rec)
    assert run(["approve", ex.name]) == 0
    head = tasks._header(str(ex))
    assert head["approved"] == "yes" and len(head["approved-hash"]) == 64
    assert run(["keep", ex.name]) == 0
    assert run(["read", ex.name, "--reason", "r"]) == 0
    ex.write_text(ex.read_text() + "added line\n")
    capsys.readouterr()
    assert run(["read", ex.name, "--reason", "r"]) == 2
    assert "changed since approval" in capsys.readouterr().err


def test_extract_flag_alone_and_changed_source(rec, capsys):
    ex = _extract(rec)
    ex.write_text(ex.read_text().replace("# approved: no", "# approved: yes"))
    capsys.readouterr()
    assert run(["read", ex.name, "--reason", "r"]) == 2
    assert "without an integrity hash" in capsys.readouterr().err
    ex.unlink()
    ex = _extract(rec)
    run(["approve", ex.name])
    (rec / host.INBOX / "multi.txt").write_text("different")
    capsys.readouterr()
    assert run(["read", ex.name, "--reason", "r"]) == 2
    assert "changed since fields ran" in capsys.readouterr().err


# ---------------------------------------------------------------- the user's confirmation
def test_cancelled_dialog_writes_nothing(rec, monkeypatch, capsys):
    t = _task(rec)
    seen = []
    monkeypatch.setattr(approval, "ask_desktop", lambda title, text: seen.append(text) or False)
    capsys.readouterr()
    assert run(["approve", t.name]) == 2
    assert "the dialog was cancelled" in capsys.readouterr().err
    assert json.loads(t.read_text())["approved"] is False
    text = seen[0]
    assert "paystub" in text and "documents: 1" in text and "flagged:" in text
    assert text.endswith(approval.QUESTION) and "the assistant" in approval.QUESTION


def test_no_display_refuses_without_terminal_confirm(rec, monkeypatch, capsys):
    t = _task(rec)
    monkeypatch.setattr(approval, "ask_desktop", REAL_ASK)  # DISPLAY and WAYLAND_DISPLAY are unset (conftest)
    capsys.readouterr()
    assert run(["approve", t.name]) == 2
    assert "no desktop dialog could be shown" in capsys.readouterr().err
    assert json.loads(t.read_text())["approved"] is False


def test_real_dialog_runs_zenity_and_honours_its_exit(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(approval, "dialog_tool", lambda: ("zenity", "/usr/bin/zenity"))
    calls = []

    def fake(code):
        def run_(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, code)
        return run_

    assert REAL_ASK("t", "x", run=fake(0)) is True
    assert REAL_ASK("t", "x", run=fake(1)) is False
    assert REAL_ASK("t", "x", run=fake(5)) is False  # timeout
    argv = calls[0]
    assert argv[0] == "/usr/bin/zenity" and "--question" in argv and "--default-cancel" in argv
    assert argv[argv.index("--ok-label") + 1] == "Approve" and "--no-markup" in argv


def test_dialog_tool_ignores_path(monkeypatch, tmp_path):
    fake = tmp_path / "zenity"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    tool = approval.dialog_tool()
    assert tool is None or tool[1].startswith(("/usr/bin/", "/bin/"))


def test_accepted_dialog_approves(rec, monkeypatch):
    t = _task(rec)
    monkeypatch.setattr(approval, "ask_desktop", lambda title, text: True)
    assert run(["approve", t.name]) == 0
    assert "via=desktop" in (rec / host.STATE / "access.log").read_text()


def _tty(answer, shown):
    return lambda: contextlib.nullcontext((shown.append, lambda: answer(shown)))


def test_terminal_confirm_phrase(rec, monkeypatch, capsys):
    t = _task(rec)
    monkeypatch.setattr(approval, "ask_desktop", lambda title, text: None)
    monkeypatch.setattr(approval, "phrase", lambda n=4: "maple otter quill river")
    shown = []
    monkeypatch.setattr(approval, "tty_io", _tty(lambda s: "maple otter quill rivet\n", shown))
    capsys.readouterr()
    assert run(["approve", t.name, "--terminal-confirm"]) == 2
    assert "phrase did not match" in capsys.readouterr().err
    assert "type: maple otter quill river" in shown[0] and "WEAKER" in shown[0]
    assert json.loads(t.read_text())["approved"] is False
    monkeypatch.setattr(approval, "tty_io", _tty(lambda s: "  Maple otter  quill river\n", []))
    assert run(["approve", t.name, "--terminal-confirm"]) == 0
    assert run(["read", t.name, "--reason", "r"]) == 0
    assert "via=terminal" in (rec / host.STATE / "access.log").read_text()


def test_terminal_confirm_needs_a_controlling_terminal(rec, monkeypatch, capsys):
    t = _task(rec)
    monkeypatch.setattr(approval, "ask_desktop", lambda title, text: None)
    capsys.readouterr()
    assert run(["approve", t.name, "--terminal-confirm"]) == 2  # conftest: no /dev/tty
    assert json.loads(t.read_text())["approved"] is False


def test_cancel_wins_over_terminal_confirm(rec, monkeypatch, capsys):
    t = _task(rec)
    monkeypatch.setattr(approval, "ask_desktop", lambda title, text: False)
    assert run(["approve", t.name, "--terminal-confirm"]) == 2
    assert json.loads(t.read_text())["approved"] is False


def test_tty_check_still_applies(rec, capsys):
    t = _task(rec)
    assert host.main(["approve", t.name]) == 2  # rec: is_tty() is False, as in the assistant's shell
    assert json.loads(t.read_text())["approved"] is False


def test_phrase_is_four_listed_words():
    words = approval.phrase().split()
    assert len(words) == 4 and all(w in approval.WORDS for w in words)
    assert len(set(approval.WORDS)) == len(approval.WORDS) >= 128


def test_approved_task_for_finds_a_document_inside_an_approved_batch(rec):
    drop(rec, "a.txt", "ACME Hardware\n2030-03-01\nTOTAL $12.34\n")
    drop(rec, "b.txt", "Example Supply\n2030-04-02\nTOTAL $56.78\n")
    run(["fields", "--task", "receipt", "inbox/a.txt", "inbox/b.txt"])
    (t,) = (rec / host.STATE / "tasks").iterdir()
    run(["approve", t.name])
    body = json.loads(t.read_text())
    assert len(body["sources"]) == 2
    for src in body["sources"]:
        path, one = tasks.approved_task_for(src["sha256"])
        assert path == str(t) and one["sources"] == [src]
        assert one["values"] and all(x["file"] == src["file"] for x in one["values"])
    edit(t, lambda b: b["values"].append({**b["values"][0], "field": "tax"}))
    assert all(tasks.approved_task_for(s["sha256"]) is None for s in body["sources"])


class _FakeTty:
    """Stands in for /dev/tty: records what was written and whether it was closed."""
    def __init__(self, answer):
        self.answer, self.written, self.closed = answer, [], False

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def readline(self):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def _open_tty(fake, opened):
    def fake_open(path, *a, **kw):
        opened.append(path)
        return fake
    return fake_open


def test_tty_io_without_a_terminal_is_an_approval_error(monkeypatch):
    def no_tty(path, *a, **kw):
        raise OSError("no controlling terminal")
    monkeypatch.setattr("builtins.open", no_tty)
    with pytest.raises(approval.ApprovalError, match="controlling terminal"):
        with REAL_TTY_IO():
            pass


def test_tty_io_closes_the_terminal_after_the_phrase(monkeypatch):
    fake, opened = _FakeTty("maple otter quill river\n"), []
    monkeypatch.setattr("builtins.open", _open_tty(fake, opened))
    monkeypatch.setattr(approval, "phrase", lambda n=4: "maple otter quill river")
    assert approval.ask_terminal("Approve?", io=REAL_TTY_IO) is True
    assert opened == ["/dev/tty"] and fake.closed


def test_tty_io_closes_the_terminal_when_reading_fails(monkeypatch):
    fake, opened = _FakeTty(KeyboardInterrupt()), []
    monkeypatch.setattr("builtins.open", _open_tty(fake, opened))
    with pytest.raises(KeyboardInterrupt):
        approval.ask_terminal("Approve?", io=REAL_TTY_IO)
    assert fake.closed
