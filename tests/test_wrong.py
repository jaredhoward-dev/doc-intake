"""Marking a value wrong (`wrong`): the rejected candidate is kept out of a re-run of that one document, the result is a
new unapproved task (an approved one is superseded, never edited), and only the user's terminal may do it.

Through the fake host (tests/host.py) against a throwaway records root. Every document here is made up."""
import html
import json

import pytest

import host
from doc_intake import places, preview
from helpers import ACCOUNT, FAKES, drop, rec, run  # noqa: F401  (rec is a fixture)

TASKS = (host.STATE, "tasks")

# Two "Total" lines: the first one wins, so marking it wrong must take the second.
TWO_TOTALS = f"""Example Garden Shop
Receipt
Date: 01/09/2030
Total      $12.00
Tax        $0.72
Total      $12.72
Payment    Visa
Account {ACCOUNT}
"""

# One total, last on the page: marking it wrong leaves the field missing.
ONE_TOTAL = """Example Book Shop
Receipt
Date: 02/03/2030
Tax        $0.30
Payment    Visa
Total      $5.30
"""

KNOWN = """Example Cloud Services
Receipt
Date: 03/04/2030
Total      $7.00
"""


def _wrong(argv, tty=True):
    """wrong, run as if at the user's own terminal (it refuses anywhere else) unless tty is False."""
    orig = places.is_tty
    places.is_tty = lambda: tty
    try:
        return host.main(["wrong", *argv])
    finally:
        places.is_tty = orig


def _tasks(rec):
    return sorted(rec.joinpath(*TASKS).iterdir())


def _load(p):
    return json.loads(p.read_text())


def _vals(t, doc=0):
    src = t["sources"][doc]["file"]
    return {v["field"]: v for v in t["values"] if v["file"] == src}


def _fields(rec, *names, files=None):
    for n in names:
        drop(rec, f"{n}.txt", {"two": TWO_TOTALS, "one": ONE_TOTAL, "known": KNOWN}[n])
    assert run(["fields", "--task", "receipt", *(files or [f"inbox/{n}.txt" for n in names])]) == 0
    (t,) = _tasks(rec)
    return t


def test_wrong_takes_the_next_candidate(rec):
    t = _fields(rec, "two", "one")
    before = _load(t)
    assert _vals(before)["total"]["value"] == "$12.00"
    assert _wrong([t.name, "--doc", "1", "--field", "receipt.total", "--reason", "it is the subtotal"]) == 0
    (same,) = _tasks(rec)  # an unapproved task is re-written in place, as fields --replaces does
    after = _load(same)
    assert after["approved"] is False and "approved_hash" not in after
    assert _vals(after)["total"]["value"] == "$12.72"
    assert _vals(after, 1) == _vals(before, 1)  # the other document is carried over unchanged
    (rej,) = after["rejected"]
    assert set(rej) == {"sha256", "group", "plan", "row", "field", "page", "label", "span"}  # never the value
    assert rej["field"] == "total" and rej["sha256"] == before["sources"][0]["sha256"]
    assert "$12.00" not in json.dumps(rej)
    log = rec.joinpath(host.STATE, "access.log").read_text()
    assert "\twrong\t" in log and "field=receipt.total" in log and "reason=it is the subtotal" in log


def test_wrong_twice_leaves_the_field_missing_and_keeps_both_out(rec):
    t = _fields(rec, "one")
    assert _vals(_load(t))["total"]["value"] == "$5.30"
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    after = _load(t)
    assert "total" not in _vals(after) and "receipt.total" in after["missing"]
    assert after["missing"].count("receipt.total") == 1
    # a later fields --replaces of the same task still keeps the marked value out
    assert run(["fields", "--task", "receipt", "--replaces", t.name, "inbox/one.txt"]) == 0
    again = _load(t)
    assert "total" not in _vals(again) and again["rejected"] == after["rejected"]


def test_rejections_accumulate(rec):
    t = _fields(rec, "two")
    assert _wrong([t.name, "--doc", "1", "--field", "receipt.payment-method"]) == 0  # dashes or underscores
    assert "payment_method" not in _vals(_load(t)) or _vals(_load(t))["payment_method"]["label"] != "payment"
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    after = _load(t)
    assert len(after["rejected"]) == 3
    assert _vals(after).get("total", {}).get("value") not in ("$12.00", "$12.72")


def test_known_sender_has_no_spot_but_is_still_kept_out(rec):
    t = _fields(rec, "known")
    assert _vals(_load(t))["vendor"]["label"] == "known sender"
    assert _wrong([t.name, "--doc", "1", "--field", "vendor"]) == 0
    v = _vals(_load(t)).get("vendor")
    assert v is None or v["label"] != "known sender"


def test_approved_task_is_superseded_not_edited(rec, capsys):
    t = _fields(rec, "two")
    assert run(["approve", t.name]) == 0
    sealed = t.read_text()
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    assert len(_tasks(rec)) == 2
    old, new = t, t.with_name(t.name[:-5] + "-v2.json")
    o, n = _load(old), _load(new)
    assert o["superseded_by"] == new.name and o["approved"] is True
    assert {k: x for k, x in o.items() if k not in ("superseded_by", "superseded_at")} == json.loads(sealed)
    assert n["approved"] is False and "approved_hash" not in n and n["replaces"] == t.name
    assert _vals(n)["total"]["value"] == "$12.72"
    capsys.readouterr()
    assert run(["read", old.name, "--reason", "test"]) == 2  # superseded
    assert run(["read", new.name, "--reason", "test"]) == 2  # not approved: approval is never carried over
    assert _wrong([old.name, "--doc", "1", "--field", "total"]) == 2  # already superseded
    assert "superseded" in capsys.readouterr().err


def test_refused_without_a_terminal(rec, capsys):
    t = _fields(rec, "two")
    body = t.read_text()
    assert _wrong([t.name, "--doc", "1", "--field", "total"], tty=False) == 2
    assert "own terminal" in capsys.readouterr().err
    assert t.read_text() == body and _tasks(rec) == [t]


@pytest.mark.parametrize("argv, msg", [
    (["--doc", "3", "--field", "total"], "--doc takes 1 to 1"),
    (["--doc", "0", "--field", "total"], "--doc takes 1 to 1"),
    (["--doc", "1", "--field", "colour"], "no value for colour"),
    (["--doc", "1", "--field", "invoice.total"], "no value for invoice.total"),
])
def test_bad_doc_or_field_is_a_clear_error(rec, capsys, argv, msg):
    t = _fields(rec, "two")
    body = t.read_text()
    capsys.readouterr()
    assert _wrong([t.name, *argv]) == 2
    err = capsys.readouterr().err
    assert msg in err and "$" not in err  # field names only, never a value
    assert t.read_text() == body


def test_changed_document_is_refused(rec, capsys):
    t = _fields(rec, "two")
    (rec / "inbox" / "two.txt").write_text(TWO_TOTALS.replace("12.72", "12.73"))
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 2
    assert "changed since" in capsys.readouterr().err


def test_page_shows_the_command_per_value(rec, monkeypatch):
    monkeypatch.setattr(preview, "has_display", lambda: False)
    t = _fields(rec, "two", "one")
    assert run(["preview", "--html", "--no-open", t.name]) == 0
    page = rec.joinpath(host.STATE, "previews", t.name[:-5] + ".html").read_text()
    for doc, field in ((1, "total"), (1, "date"), (2, "total"), (2, "vendor"), (1, "payment-method")):
        cmd = f'{host.CLI} wrong "{t.name}" --doc {doc} --field receipt.{field}'
        assert html.escape(cmd) in page
    assert "<script" not in page and "payment_method" not in page  # dashes: the page shows no internal names


def test_wrong_command_qualifies_plan_and_row():
    rec_ = {"group": "in_network", "field": "out_of_pocket", "plan": "Gold PPO", "row": 2}
    assert preview.wrong_command("x.json", 3, rec_) == (
        f"{host.CLI} wrong \"x.json\" --doc 3 --field in-network.out-of-pocket --plan 'Gold PPO' --row 2")


def test_no_sensitive_value_in_output_or_task(rec, capsys):
    t = _fields(rec, "two")
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    out = capsys.readouterr()
    text = out.out + out.err + t.read_text() + rec.joinpath(host.STATE, "access.log").read_text()
    for f in FAKES:
        assert f not in text
