"""Golden run: scan, fields, preview, approve, pairs, status, read, extract, keep and cleanup over a synthetic records
folder, through the fake host's CLI (tests/host.py).

The fixture (tests/fixtures/golden/records/, written by tests/fixtures/golden/make_fixture.py) is copied into a
throwaway records root. Every command's output, and a summary of the state it leaves (index.json, records-hashes.json,
the task files, the review pages, the access log, the file tree), must match tests/fixtures/golden/expected/ exactly,
once timestamps, today's date, the approval hash and the temporary root are replaced by placeholders. It pins the
engine's behaviour across refactors: a change here is a change users see.

Task files and review pages hold every extracted value, so they are pinned as summaries (field names, counts,
badges), never their values; the only values in expected/ are what the engine prints for an approved read.

    GOLDEN_UPDATE=1 python3 -I -m pytest -q tests/test_golden.py   # rewrite expected/ after an intended change
"""
import datetime as dt
import json
import os
import re
import shutil

import pytest

import doc_intake
from doc_intake import reader
from helpers import rec, run  # noqa: F401  (rec is a fixture)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "golden", "records")
EXPECTED = os.path.join(HERE, "fixtures", "golden", "expected")
STAMP = dt.datetime(2030, 1, 1, 12, 0, 0).timestamp()  # every fixture file's mtime: "date from file" is stable
# The engine's "today" for judging document dates (config.today), so the run doesn't drift with the calendar. Late
# 2029 puts the fixture's January dates within 60 days and its February and March dates past that: the run pins
# both a plausible date and a flagged one. File names and timestamps stay on the real clock (<TODAY>, <NOW>).
TODAY = dt.date(2029, 12, 1)



def _normalize(text, root):
    today = dt.date.today().isoformat()
    for r in sorted({root, os.path.realpath(root)}, key=len, reverse=True):
        text = text.replace(r, "<ROOT>")
    text = re.sub(r'("approved_hash": ")[0-9a-f]{64}"', r'\1<HASH>"', text)
    text = re.sub(r"(# approved-hash: )[0-9a-f]{64}", r"\1<HASH>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?", "<NOW>", text)
    return text.replace(today, "<TODAY>")


def _run(capsys, argv):
    rc = run(argv)  # approve runs as if at the user's own terminal; nothing else does
    got = capsys.readouterr()
    return f"$ host.py {' '.join(argv)}\nrc={rc}\n--- stdout\n{got.out}--- stderr\n{got.err}"


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _task_summary(text):
    """A task file without its values: its keys, its sources, the field names per document, the counts."""
    t = json.loads(text)
    per = {}
    for v in t.get("values", []):
        per.setdefault(v.get("file"), []).append(f'{v.get("group")}.{v.get("field")}'
                                                 + (f'[{v["row"]}]' if v.get("row") is not None else ""))
    out = {"keys": sorted(t), "task": t.get("task"), "approved": t.get("approved"), "keep": t.get("keep"),
           "sources": [s.get("file") for s in t.get("sources", [])], "fields": per,
           "missing": t.get("missing"), "dropped": t.get("dropped")}
    return json.dumps(out, indent=1, sort_keys=True) + "\n"


def _page_summary(html):
    """A review page without its values: cards, which start open, and the badges and warnings it shows."""
    cards = re.findall(r'<details class="card"[^>]*?( open)?>', html)
    badges = re.findall(r'<span class="badge [^"]+">([^<]+)</span>', html)
    out = {"cards": len(cards), "open": sum(1 for c in cards if c), "badges": sorted(badges),
           "check_warn": html.count('class="check-warn"'), "pair_alert": 'class="alert"' in html,
           "approve_hint": bool(re.search(r"<pre>python3 tests/host\.py approve ", html))}
    return json.dumps(out, indent=1, sort_keys=True) + "\n"


def _hashes_summary(text):
    """records-hashes.json without its fingerprints: which files, their sizes, and whether text was fingerprinted."""
    files = json.loads(text)["files"]
    return "".join(f'{k}  size={v["size"]}  text_fp={"yes" if v.get("text_fp") else "no"}\n'
                   for k, v in sorted(files.items()))


def golden_outputs(root, capsys):
    root = str(root)
    for dirpath, _dirs, names in os.walk(FIXTURE):
        rel = os.path.relpath(dirpath, FIXTURE)
        os.makedirs(os.path.join(root, rel), mode=0o700, exist_ok=True)
        for n in names:
            shutil.copyfile(os.path.join(dirpath, n), os.path.join(root, rel, n))
    for dirpath, _dirs, names in os.walk(root):  # one mtime and one mode, whatever umask an earlier test left
        os.chmod(dirpath, 0o700)
        for n in names:
            os.chmod(os.path.join(dirpath, n), 0o600)
            os.utime(os.path.join(dirpath, n), (STAMP, STAMP))
    out = {}
    out["01-scan"] = _run(capsys, ["scan", "--year", "2030"])
    tasks = os.path.join(root, ".doc-intake", "tasks")
    runs = [("receipt", ["inbox/receipt-software.txt", "inbox/receipt-print.txt", "inbox/garden-order.eml"]),
            ("invoice", ["inbox/invoice-software.txt", "inbox/invoice-print.txt"]),
            ("tax_w2", ["inbox/w2-2030.txt"]),
            ("paystub", ["inbox/paystub.txt"])]
    names = []
    for i, (task, files) in enumerate(runs):
        before = set(os.listdir(tasks)) if os.path.isdir(tasks) else set()
        out[f"02-fields-{i}-{task}"] = _run(capsys, ["fields", "--task", task] + files)
        (new,) = set(os.listdir(tasks)) - before
        names.append(new)
    out["03-fields-labels"] = _run(capsys, ["fields", "--task", "invoice", "--labels", "inbox/invoice-print.txt"])
    for n in names:
        out[f"04-preview-{n}"] = _run(capsys, ["preview", "--html", "--no-open", n])
        out[f"04-preview-{n}.page"] = _page_summary(
            _read(os.path.join(root, ".doc-intake", "previews", n[:-5] + ".html")))
    out["05-pairs-before-approval"] = _run(capsys, ["pairs"])
    for n in names[:3]:
        out[f"06-approve-{n}"] = _run(capsys, ["approve", n])
    out["07-pairs"] = _run(capsys, ["pairs"])
    out["07-pairs-one"] = _run(capsys, ["pairs", names[1]])
    out["08-status"] = _run(capsys, ["status"])
    out["08-status-detail"] = _run(capsys, ["status", "--detail"])
    out["09-read"] = _run(capsys, ["read", names[2], "--reason", "golden test"])
    out["09-read-unapproved"] = _run(capsys, ["read", names[3], "--reason", "golden test"])
    out["09-preview-terminal-refused"] = _run(capsys, ["preview", names[0]])
    out["09-extract"] = _run(capsys, ["extract", "inbox/paystub.txt", "--pages", "1"])
    (ext,) = os.listdir(os.path.join(root, ".doc-intake", "extracts"))
    out["09-extract-approve"] = _run(capsys, ["approve", ext])
    out["09-extract-read"] = _run(capsys, ["read", ext, "--reason", "golden extract"])
    out["09-keep"] = _run(capsys, ["keep", names[3]])
    out["09-cleanup"] = _run(capsys, ["cleanup", "--older-than", "0", "--dry-run"])
    out["10-rescan"] = _run(capsys, ["scan", "--year", "2030"])
    for n in names:
        out[f"11-task-{n}"] = _task_summary(_read(os.path.join(tasks, n)))
    state = os.path.join(root, ".doc-intake")
    out["12-state-index.json"] = _read(os.path.join(state, "index.json"))
    out["12-state-records-hashes"] = _hashes_summary(_read(os.path.join(state, "records-hashes.json")))
    out["12-state-access.log"] = _read(os.path.join(state, "access.log"))
    out["13-tree"] = "\n".join(sorted(
        os.path.relpath(os.path.join(d, n), root) + f"  {oct(os.stat(os.path.join(d, n)).st_mode & 0o777)}"
        for d, _, ns in os.walk(root) for n in ns)) + "\n"
    return {k: _normalize(v, root) for k, v in out.items()}


def test_golden_run(rec, capsys):
    doc_intake.configure(today=lambda: TODAY)  # fresh_config (conftest) puts the real calendar back afterwards
    got = golden_outputs(rec, capsys)
    today = dt.date.today().isoformat()
    got = {k.replace(os.sep, "_").replace(today, "TODAY"): v for k, v in got.items()}
    if os.environ.get("GOLDEN_UPDATE"):
        if os.path.isdir(EXPECTED):
            shutil.rmtree(EXPECTED)
        os.makedirs(EXPECTED)
        for k, v in got.items():
            with open(os.path.join(EXPECTED, k + ".txt"), "w", encoding="utf-8") as f:
                f.write(v)
    want = {n[:-4]: _read(os.path.join(EXPECTED, n)) for n in sorted(os.listdir(EXPECTED))}
    assert sorted(got) == sorted(want)
    for k in sorted(want):
        assert got[k] == want[k], k
    # the run must exercise every path it is meant to pin
    assert "HELD logins.txt" in want["01-scan"] and "duplicate of" in want["01-scan"]
    assert "possible duplicate of" in want["01-scan"] and "MISMATCH" in want["07-pairs"]
    tasks = [json.loads(v) for k, v in want.items() if k.startswith("11-task-")]
    assert sum(t["approved"] is True for t in tasks) == 3 and len(tasks) == 4
    assert "golden test" in want["12-state-access.log"]
    # dates are judged against the pinned TODAY: some within 60 days of it, some past that and flagged
    detail = want["08-status-detail"]
    assert re.search(r"(?m)^receipt-\S+: .*flagged date: outside 2000 to 60 days from today \(2\)", detail)
    assert re.search(r"(?m)^paystub-\S+: .*; flagged none;", detail)
