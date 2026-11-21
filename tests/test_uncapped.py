"""Every command that reads documents says once, at its end, how many files it read without the memory cap.

On macOS the bounded child can't lower its address-space limit and reads uncapped (reader.cap_address_space);
here the cap is forced to apply or to fail, so the note is checked on every platform. scan has its own test
(test_scan_memory.py). Through the fake host against a throwaway records root; every document is made up."""
import pytest

import host
from doc_intake import reader
from helpers import drop, rec, run  # noqa: F401  (rec is a fixture)

NOTE = "read without a memory cap (this platform refuses it); the timeout and pixel caps still applied."
SENTINEL = "zebrafinch"


def receipts(root):
    drop(root, "a.txt", f"Example Shop\n2030-03-01\nTOTAL $12.34\n{SENTINEL}\n")
    drop(root, "b.txt", f"Sample Market\n2030-03-02\nTOTAL $5.00\n{SENTINEL}\n")
    return ["inbox/a.txt", "inbox/b.txt"]


def cap(monkeypatch, applies):
    monkeypatch.setattr(reader, "cap_address_space", lambda nbytes: applies)


def said(out, n):
    """The note appears exactly once, with n files (fields and extract read each file twice: counted once)."""
    noun = "file" if n == 1 else "files"
    return out.count(NOTE) == 1 and f"\n{n} {noun} {NOTE}" in out


def task_name(root):
    (t,) = (root / host.STATE / "tasks").iterdir()
    return t.name


@pytest.mark.parametrize("applies", [True, False])
def test_fields_says_once(rec, monkeypatch, capsys, applies):
    cap(monkeypatch, applies)
    assert run(["fields", "--task", "receipt"] + receipts(rec)) == 0
    out = capsys.readouterr().out
    assert (NOTE not in out) if applies else said(out, 2)
    assert SENTINEL not in out


@pytest.mark.parametrize("applies", [True, False])
def test_extract_says_once(rec, monkeypatch, capsys, applies):
    cap(monkeypatch, applies)
    assert run(["extract", "--pages", "1"] + receipts(rec)) == 0
    out = capsys.readouterr().out
    assert (NOTE not in out) if applies else said(out, 2)
    assert SENTINEL not in out


@pytest.mark.parametrize("applies", [True, False])
def test_preview_html_says_once(rec, monkeypatch, capsys, applies):
    cap(monkeypatch, True)
    assert run(["fields", "--task", "receipt"] + receipts(rec)) == 0
    capsys.readouterr()
    cap(monkeypatch, applies)
    assert run(["preview", "--html", "--no-open", task_name(rec)]) == 0
    out = capsys.readouterr().out
    assert (NOTE not in out) if applies else said(out, 2)
    assert SENTINEL not in out


def test_each_command_starts_from_zero(rec, monkeypatch, capsys):
    cap(monkeypatch, False)
    files = receipts(rec)
    assert run(["fields", "--task", "receipt"] + files) == 0
    assert said(capsys.readouterr().out, 2)
    assert run(["extract", "--pages", "1", files[0]]) == 0
    assert said(capsys.readouterr().out, 1)  # not 3: extract reset the count fields left behind
    assert run(["preview", "--html", "--no-open", task_name(rec)]) == 0
    assert said(capsys.readouterr().out, 2)
    cap(monkeypatch, True)
    assert run(["extract", "--pages", "1", files[1]]) == 0
    assert NOTE not in capsys.readouterr().out
