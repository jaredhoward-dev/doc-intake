"""Pair agreement (doc_intake.pairs): an approved invoice and an approved receipt for one charge, listed by the fake
host's pairs command, the matching rules, which task files count, and the preview's pair warnings.

A host's filing step (refusing a mismatched half, booking one ledger row for the pair) is the host's own and is not
tested here. A throwaway records root only; every vendor, date and amount is fake."""
import datetime as dt

import pytest

from doc_intake import pairs, reader
from doc_intake.config import CFG
from helpers import drop, rec, run  # noqa: F401  (rec is a fixture)

RECEIPT = """SAMPLE SOFTWARE
Receipt
01/09/2030
Monthly plan     9.00
Tax              0.54
TOTAL            $9.54
Thank you
"""
INVOICE = """SAMPLE SOFTWARE
Invoice
Invoice number EX-1001
Invoice date 01/08/2030
Monthly plan     9.00
Tax              0.54
Amount due       $9.54
"""



def tasks_dir(root):
    return root / ".doc-intake" / "tasks"


def fields(root, name, text, task):
    """A fake document in the inbox and its fields pulled into a new task file; returns the task file's name."""
    drop(root, name, text)
    d = tasks_dir(root)
    before = set(d.glob("*.json")) if d.exists() else set()
    assert run(["fields", "--task", task, f"inbox/{name}"]) == 0
    (t,) = set(d.glob("*.json")) - before
    return t.name


def approved(root, name, text, task):
    t = fields(root, name, text, task)
    assert run(["approve", t]) == 0
    return t


def test_pairs_lists_an_agreeing_pair(rec, capsys):
    approved(rec, "receipt.txt", RECEIPT, "receipt")
    approved(rec, "invoice.txt", INVOICE, "invoice")
    capsys.readouterr()
    assert run(["pairs"]) == 0
    o = capsys.readouterr().out
    assert "pair agree: Sample Software; invoice 2030-01-08 9.54 = receipt 2030-01-09 9.54" in o
    assert "invoice.txt + receipt.txt (by vendor and date)" in o
    assert "1 pair(s), 0 mismatch(es), 0 unmatched" in o
    assert "EX-1001" not in o  # the number matched nothing here and is never printed


def test_pairs_flags_a_mismatch(rec, capsys):
    approved(rec, "receipt.txt", RECEIPT.replace("$9.54", "$9.45"), "receipt")
    approved(rec, "invoice.txt", INVOICE, "invoice")
    capsys.readouterr()
    assert run(["pairs"]) == 1
    o = capsys.readouterr().out
    assert "pair MISMATCH: Sample Software; invoice 2030-01-08 9.54 != receipt 2030-01-09 9.45" in o
    assert "1 pair(s), 1 mismatch(es), 0 unmatched" in o


def test_pairs_leave_out_unapproved_task_files(rec, capsys):
    approved(rec, "receipt.txt", RECEIPT, "receipt")
    fields(rec, "invoice.txt", INVOICE, "invoice")  # pulled, not approved
    capsys.readouterr()
    assert run(["pairs"]) == 0
    o = capsys.readouterr().out
    assert "unmatched receipt: Sample Software 2030-01-09 9.54; receipt.txt" in o
    assert "0 pair(s), 0 mismatch(es), 1 unmatched" in o


def doc(kind, sha, vendor, date, total, numbers=()):
    return {"task": sha + ".json", "kind": kind, "file": sha + ".pdf", "sha256": sha, "vendor": vendor,
            "vkey": pairs.vendor_key(vendor), "date": dt.date.fromisoformat(date), "total": pairs._money(total),
            "created": "", "numbers": set(numbers)}


def test_pair_matching_rules():
    docs_ = [doc("invoice", "i1", "Example Cloud", "2030-02-01", "20.00"),
             doc("invoice", "i2", "Example Cloud", "2030-02-01", "35.00"),
             doc("receipt", "r1", "EXAMPLE CLOUD", "2030-02-03", "35.00"),
             doc("receipt", "r2", "Example Cloud", "2030-02-04", "20.00"),
             doc("receipt", "r3", "Example Cloud", "2030-02-05", "20.00"),       # 4 days from the invoices
             doc("invoice", "i3", "Demo Print Shop", "2030-03-01", "5.00", {"AB12"}),
             doc("receipt", "r4", "Demo Print Shop LLC", "2030-03-20", "5.00", {"AB12"})]  # far apart, same number
    found, left = pairs.match(docs_)
    got = {(p["invoice"]["sha256"], p["receipt"]["sha256"], p["agree"], p["how"]) for p in found}
    assert got == {("i1", "r2", True, "vendor and date"), ("i2", "r1", True, "vendor and date"),
                   ("i3", "r4", True, "number")}
    assert [d["sha256"] for d in left] == ["r3"]
    p, other = pairs.pair_for("r2", docs_)
    assert p["invoice"]["sha256"] == "i1" and other["sha256"] == "i1"
    assert pairs.pair_for("r3", docs_) == (None, None)


def test_vendor_key_default_and_host_override(monkeypatch):
    assert pairs.vendor_key("Example Cloud, Inc.") == "examplecloudinc"
    assert pairs.vendor_key(None) == ""
    monkeypatch.setattr(CFG, "vendor_key", lambda name: (name or "").split()[0].lower())
    assert pairs.vendor_key("Example Cloud, Inc.") == pairs.vendor_key("EXAMPLE CO") == "example"


def _task(kind, sha, total, approved=True, created="2030-01-10T10:00:00", **extra):
    return {"task": kind, "approved": approved, "created": created,
            "sources": [{"file": f"inbox/{sha}.txt", "sha256": sha}],
            "values": [{"field": "vendor", "value": "Sample Software", "file": f"inbox/{sha}.txt"},
                       {"field": "date", "value": "2030-01-09", "file": f"inbox/{sha}.txt"},
                       {"field": "total", "value": total, "file": f"inbox/{sha}.txt"},
                       {"field": "receipt_number", "value": "ab-12", "file": f"inbox/{sha}.txt"}], **extra}


def test_documents_takes_approved_current_invoice_and_receipt_tasks():
    tasks = [("a.json", _task("receipt", "s1", "$9.54")),
             ("b.json", _task("receipt", "s2", "9.54", approved=False)),           # not approved
             ("c.json", _task("receipt", "s3", "9.54", superseded_by="d.json")),   # replaced
             ("e.json", _task("paystub", "s4", "9.54")),                           # not a pair kind
             ("f.json", _task("receipt", "s1", "$1,009.54", created="2030-01-11T10:00:00"))]  # newer, same document
    got = pairs.documents(tasks)
    assert [(d["task"], d["sha256"], str(d["total"]), d["numbers"]) for d in got] == [
        ("f.json", "s1", "1009.54", {"AB12"})]
    got = pairs.documents(tasks, include={"b.json"})  # the preview's own, unapproved task
    assert sorted(d["task"] for d in got) == ["b.json", "f.json"]


def test_describe_never_prints_more_than_vendor_date_total_and_file():
    d = doc("receipt", "r1", "", "2030-02-03", "35.00", {"SECRET1"})
    d["date"], d["total"] = None, None
    assert pairs.describe_doc(d) == "unmatched receipt: ? ? ?; r1.pdf"


@pytest.fixture
def pair_checks(monkeypatch):
    """A host's preview_checks: warn on a receipt or invoice whose pair total differs (the way a real host wires
    pairs into the review page)."""
    from doc_intake.tasks import _task_files

    def checks(task, name, src, vals):
        p, _other = pairs.pair_for(src.get("sha256"), pairs.documents(_task_files(), include={name}))
        return ([pairs.describe(p)] if p and not p["agree"] else []), []
    monkeypatch.setattr(CFG, "preview_checks", checks)


def test_preview_html_shows_a_pair_mismatch(rec, pair_checks):
    approved(rec, "receipt.txt", RECEIPT.replace("$9.54", "$9.45"), "receipt")
    t = fields(rec, "invoice.txt", INVOICE, "invoice")
    assert run(["preview", "--html", "--no-open", t]) == 0
    page = (rec / ".doc-intake" / "previews" / (t[:-5] + ".html")).read_text()
    assert 'class="check-warn"' in page and "pair MISMATCH" in page and "1 amount check(s)" in page
    assert "disagrees with its invoice or receipt pair" in page  # the engine's own pair check
