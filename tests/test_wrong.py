"""Marking a value wrong (`wrong`): the rejected candidate is kept out of a re-run of that one document, the result is a
new unapproved task (an approved one is superseded, never edited), and only the user's terminal may do it.

Through the fake host (tests/host.py) against a throwaway records root. Every document here is made up."""
import html
import json

import pytest

import host
from doc_intake import places, preview, tasks
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


def _wrong(argv, tty=True, stdin=None):
    """wrong, run as if at the user's own terminal (stdout and stdin; it refuses anywhere else) unless tty is False;
    stdin, when given, sets stdin's answer on its own."""
    orig, orig_in = places.is_tty, tasks.stdin_is_tty
    places.is_tty = lambda: tty
    tasks.stdin_is_tty = lambda: tty if stdin is None else stdin
    try:
        return host.main(["wrong", *argv])
    finally:
        places.is_tty, tasks.stdin_is_tty = orig, orig_in


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


# ---------------------------------------------------------------- round 4 gate: the terminal, refusals, edge cases
def test_stdout_a_terminal_with_stdin_a_pipe_is_refused(rec, capsys):
    """'wrong ... > /dev/tty' from a tool runner: stdout is a terminal, stdin its pipe. Refused, task unchanged."""
    t = _fields(rec, "two")
    body = t.read_text()
    capsys.readouterr()
    assert _wrong([t.name, "--doc", "1", "--field", "total"], tty=True, stdin=False) == 2
    assert "own terminal" in capsys.readouterr().err
    assert t.read_text() == body and _tasks(rec) == [t]


BENEFITS = """2030 Open Enrollment Benefits Guide
Medical Plans            Example PPO Plan      Example HDHP Plan
Deductible (individual / family)   $500 / $1,000    $1,700 / $3,400
Employee only    $45.00    $20.00
"""


def test_a_field_per_plan_needs_plan_and_plan_narrows_it(rec, capsys):
    drop(rec, "b.txt", BENEFITS)
    assert run(["fields", "--task", "benefits", "inbox/b.txt"]) == 0
    (t,) = _tasks(rec)
    body = t.read_text()
    capsys.readouterr()
    assert _wrong([t.name, "--doc", "1", "--field", "medical.deductible-individual"]) == 2
    err = capsys.readouterr().err
    assert "2 values" in err and "add --plan or --row" in err and "$" not in err and "Example" not in err
    assert t.read_text() == body
    assert _wrong([t.name, "--doc", "1", "--field", "medical.deductible-individual",
                   "--plan", "Example PPO Plan"]) == 0
    after = _load(t)
    (rej,) = after["rejected"]
    assert rej["plan"] == "Example PPO Plan" and rej["field"] == "deductible_individual"
    got = {(v["plan"], v["field"]): v["value"] for v in after["values"]}
    assert got.get(("Example PPO Plan", "deductible_individual")) != "$500"
    assert got[("Example HDHP Plan", "deductible_individual")] == "$1,700"  # the other plan's value is untouched


TREATMENT = """Treatment Plan Estimate
Code     Tooth   Description              Fee        Ins Est    Pt Est
D2740    8       Crown porcelain          $1,200.00  $600.00    $600.00
D0120            Periodic exam            $60.00     $60.00     $0.00
"""


def test_a_field_per_row_needs_row_and_row_narrows_it(rec, capsys):
    drop(rec, "e.txt", TREATMENT)
    assert run(["fields", "--task", "treatment_estimate", "inbox/e.txt"]) == 0
    (t,) = _tasks(rec)
    group = next(v["group"] for v in _load(t)["values"] if v["field"] == "fee")
    capsys.readouterr()
    assert _wrong([t.name, "--doc", "1", "--field", "fee"]) == 2
    assert "add --plan or --row" in capsys.readouterr().err
    assert _wrong([t.name, "--doc", "1", "--field", f"{group}.fee", "--row", "2"]) == 0
    after = _load(t)
    assert after["rejected"][0]["row"] == 2
    fees = {v["row"]: v["value"] for v in after["values"] if v["field"] == "fee"}
    assert fees.get(1) == "$1,200.00" and fees.get(2) != "$60.00"


def test_profile_changed_since_is_refused(rec, capsys):
    t = _fields(rec, "two")
    body = _load(t)
    body["profile_sha256"] = "0" * 64  # as if receipt.json had been edited since fields ran
    t.write_text(json.dumps(body, indent=1))
    before = t.read_text()
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 2
    assert "profile receipt changed since" in capsys.readouterr().err
    assert t.read_text() == before and _tasks(rec) == [t]


def test_source_gone_from_the_records_is_refused(rec, capsys):
    t = _fields(rec, "two")
    body = t.read_text()
    (rec / "inbox" / "two.txt").unlink()
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 2
    assert "no longer under the records folder" in capsys.readouterr().err
    assert t.read_text() == body and _tasks(rec) == [t]


def test_a_source_that_gained_a_secret_is_refused(rec, capsys, monkeypatch):
    """A file that gains an SSN has a new sha256, so the hash check refuses it before it is even read; the secret
    check after the read (_refuse_secret) still guards a document whose detectors say otherwise."""
    from helpers import SSN
    from doc_intake import reader
    t = _fields(rec, "two")
    body = t.read_text()
    src = rec / "inbox" / "two.txt"
    original = src.read_text()
    src.write_text(original + f"SSN {SSN}\n")
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 2
    err = capsys.readouterr().err
    assert "changed since" in err and SSN not in err
    src.write_text(original)  # same bytes, same sha256: now the read happens, and the secret check runs on it
    monkeypatch.setattr(reader, "sensitive_counts", lambda doc: {"password": 1})
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 2
    assert "holds secret material" in capsys.readouterr().err
    assert t.read_text() == body


def test_a_next_candidate_on_a_detector_region_is_dropped(rec, capsys, monkeypatch):
    """Blanking a value could in principle let the extractor reach a number the detectors flag in the original
    text: such a record is never kept, and is counted as dropped. The extractor is made to return one here."""
    from doc_intake import fields as fx
    real = fx.extract

    def fake(prof, pages, src, keep_span=False):
        recs, dropped, plans = real(prof, pages, src, keep_span)
        text = pages[0][1]
        if "$12.00" not in text:  # the blanked re-read: hand back the account number's spot as the total
            s = TWO_TOTALS.index(ACCOUNT)
            for r in recs:
                if r["field"] == "total":
                    r.update(value=ACCOUNT, span=[s, s + len(ACCOUNT)], label="total")
        return recs, dropped, plans

    t = _fields(rec, "two")
    before = _load(t)["dropped"]
    monkeypatch.setattr(fx, "extract", fake)
    capsys.readouterr()
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    out = capsys.readouterr()
    after = _load(t)
    assert "total" not in _vals(after) and "receipt.total" in after["missing"]
    assert sum(after["dropped"].values()) == sum(before.values()) + 1
    assert ACCOUNT not in t.read_text() and ACCOUNT not in out.out + out.err
    assert ACCOUNT[-4:] not in json.dumps(after["values"])


def test_the_other_documents_dropped_counts_are_unchanged(rec):
    t = _fields(rec, "two", "one")
    body = _load(t)
    body["dropped"] = {"account": 2}  # what the other document gave, as fields counted it
    t.write_text(json.dumps(body, indent=1))
    assert _wrong([t.name, "--doc", "2", "--field", "total"]) == 0
    assert _load(t)["dropped"] == {"account": 2}
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    assert _load(t)["dropped"] == {"account": 2}


def _with_model_tax(t):
    """The KNOWN document's task as if the local model had filled its tax (wrong runs no model)."""
    body = _load(t)
    src = body["sources"][0]["file"]
    body["values"].append({"group": "receipt", "plan": None, "row": None, "field": "tax", "value": "$0.50",
                           "file": src, "page": 1, "label": "local model"})
    body["missing"] = [m for m in body["missing"] if m != "receipt.tax"]
    t.write_text(json.dumps(body, indent=1))


def test_a_local_model_value_survives_unless_it_is_the_one_marked(rec):
    t = _fields(rec, "known")
    _with_model_tax(t)
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    after = _load(t)
    assert _vals(after)["tax"]["value"] == "$0.50" and "receipt.tax" not in after["missing"]
    assert _wrong([t.name, "--doc", "1", "--field", "tax"]) == 0
    after = _load(t)
    assert "tax" not in _vals(after) and "receipt.tax" in after["missing"]
    assert after["rejected"][-1]["label"] == "local model"


def test_task_files_stay_private_and_wrong_is_deterministic(rec):
    from helpers import mode
    t = _fields(rec, "two")
    start = t.read_text()
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    assert mode(t) == 0o600
    first = _load(t)
    t.write_text(start)  # the same wrong on the same task again
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    again = _load(t)
    for k in ("values", "missing", "dropped", "rejected", "sources"):
        assert again[k] == first[k]
    assert run(["approve", t.name]) == 0
    assert _wrong([t.name, "--doc", "1", "--field", "total"]) == 0
    v2 = t.with_name(t.name[:-5] + "-v2.json")
    assert mode(v2) == 0o600 and mode(t) == 0o600


def test_a_missing_field_cannot_be_marked_wrong(rec, capsys):
    t = _fields(rec, "known")
    assert "receipt.tax" in _load(t)["missing"]
    body = t.read_text()
    capsys.readouterr()
    assert _wrong([t.name, "--doc", "1", "--field", "tax"]) == 2
    assert "a missing field can't be marked wrong" in capsys.readouterr().err
    assert t.read_text() == body
