"""Marking a value wrong (`wrong`): the rejected candidate is kept out of a re-run of that one document, the result is a
new unapproved task (an approved one is superseded, never edited), and only the user's terminal may do it.

Through the fake host (tests/host.py) against a throwaway records root. Every document here is made up."""
import html
import json

import pytest

import host
from doc_intake import places, preview, tasks
from helpers import ACCOUNT, FAKES, drop, run

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
    """Defence in depth: should the extractor ever hand back, as a field's next candidate, a number the detectors
    flag in the text, the record is never kept, and is counted as dropped. The extractor is made to return one."""
    from doc_intake import fields as fx
    real = fx.extract

    def fake(prof, pages, src, keep_span=False, rejected=()):
        recs, dropped, plans = real(prof, pages, src, keep_span, rejected)
        if rejected:  # the re-read with the rejection: hand back the account number's spot as the total
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


# ---------------------------------------------------------------- round 5: the runner-up takes a rejected value's place
# A known sender found by its domain only (its name is not printed, so the value has no spot to match), and another
# business name at the top of the page: marking the known sender wrong must hand the vendor to that name.
KNOWN_BY_DOMAIN = """Example Garden Supply
Receipt
Date: 03/04/2030
Total      $7.00
Questions? help@example-cloud.test
"""


def test_a_rejected_known_sender_gives_way_to_the_runner_up(rec):
    drop(rec, "kd.txt", KNOWN_BY_DOMAIN)
    assert run(["fields", "--task", "receipt", "inbox/kd.txt"]) == 0
    (t,) = _tasks(rec)
    assert _vals(_load(t))["vendor"]["label"] == "known sender"
    assert _wrong([t.name, "--doc", "1", "--field", "vendor"]) == 0
    v = _vals(_load(t))["vendor"]
    assert (v["value"], v["label"]) == ("Example Garden Supply", "top of page")
    assert "receipt.vendor" not in _load(t)["missing"]
    # marking the runner-up wrong too: no other candidate, so the field is missing, and neither comes back
    assert _wrong([t.name, "--doc", "1", "--field", "vendor"]) == 0
    after = _load(t)
    assert "vendor" not in _vals(after) and "receipt.vendor" in after["missing"]
    assert len(after["rejected"]) == 2
    assert run(["fields", "--task", "receipt", "--replaces", t.name, "inbox/kd.txt"]) == 0
    assert "vendor" not in _vals(_load(t))


def _rej(rec_, **kw):
    """A rejection of rec_ as cmd_wrong writes it (its keys and span, never its value)."""
    return {"sha256": "0" * 64, **{k: rec_.get(k) for k in tasks._REJECT_KEYS}, "span": rec_.get("span"), **kw}


def _extract(name, text, rejected=()):
    from doc_intake import fields as fx
    recs, dropped, _ = fx.extract(fx.load_profile(name), [(1, text)], "fake.txt", keep_span=True, rejected=rejected)
    return {r["field"]: r for r in recs}, dropped


# The same wrong total reached by two labels on one line and printed again below, then the right one: the value never
# comes back from another label or spot.
REPEATED = """Example Garden Shop
Receipt
Date: 01/09/2030
Grand Total   $12.00
Tax        $0.72
Total      $12.00
Total      $12.72
"""


def test_the_same_value_from_another_spot_or_label_stays_out():
    got, _ = _extract("receipt", REPEATED)
    assert got["total"]["value"] == "$12.00" and got["total"]["label"] == "grand total"  # the earliest label wins
    after, _ = _extract("receipt", REPEATED, [_rej(got["total"])])
    # "total" inside "Grand Total" (same spot, other label) and the second "Total" line (other spot) both read
    # "$12.00" for the same field: neither is the runner-up
    assert after["total"]["value"] == "$12.72"
    assert after["tax"]["value"] == got["tax"]["value"] == "$0.72"  # the rejection is per field


# A billing period with no invoice date label: the invoice date falls back to the first date anywhere, which is the
# period's start. Marking it wrong must not move the period's own dates (the old re-read blanked that spot out of
# the text, so the period start became the end date and the end went missing).
SHARED_SPOT = """Example Print Co
Invoice
Billing Period:   03/01/2030 - 03/31/2030
Total      $40.00
"""


def test_a_rejection_never_moves_another_fields_value():
    got, _ = _extract("invoice", SHARED_SPOT)
    assert got["invoice_date"]["value"] == "2030-03-01" and got["invoice_date"]["label"] == "anywhere"
    assert (got["period_start"]["value"], got["period_end"]["value"]) == ("2030-03-01", "2030-03-31")
    after, _ = _extract("invoice", SHARED_SPOT, [_rej(got["invoice_date"])])
    assert (after["period_start"]["value"], after["period_end"]["value"]) == ("2030-03-01", "2030-03-31")
    assert after["invoice_date"]["value"] == "2030-03-31"  # "anywhere" goes on to its next hit
    assert {k: v["value"] for k, v in after.items() if k != "invoice_date"} == \
        {k: v["value"] for k, v in got.items() if k != "invoice_date"}


def test_a_rejection_on_another_field_or_page_takes_nothing():
    got, _ = _extract("receipt", TWO_TOTALS)
    rej = _rej(got["total"])
    assert _extract("receipt", TWO_TOTALS, [dict(rej, field="tax")])[0]["total"]["value"] == "$12.00"
    assert _extract("receipt", TWO_TOTALS, [dict(rej, page=2, span=None)])[0]["total"]["value"] == "$12.00"
    assert _extract("receipt", TWO_TOTALS, [rej])[0]["total"]["value"] == "$12.72"


# ---------------------------------------------------------------- round 5 gaps: tables, the first-find rules, spots
ITEMS = {"name": "t", "default_group": "totals", "groups": {"totals": {"global": True}, "items": {}},
         "fields": [{"group": "totals", "name": "total_fee", "type": "money", "labels": ["total fee"]}],
         "tables": [{"group": "items", "header_min": 2, "row_requires": ["code"],
                     "columns": [{"name": "code", "type": "ref", "labels": ["code"]},
                                 {"name": "fee", "type": "money", "labels": ["fee"]}]}]}
LINE_ITEMS = """Estimate
Code      Fee
D0120     $60.00
D0140     $60.00
Total fee   $120.00
"""


def _table(rejected=()):
    from doc_intake import fields as fx
    fx.check_profile(ITEMS)
    recs, _, _ = fx.extract(ITEMS, [(1, LINE_ITEMS)], "fake.txt", keep_span=True, rejected=rejected)
    return {(r["row"], r["field"]): r for r in recs}


def test_a_table_cell_marked_wrong_is_kept_out_of_its_row_only():
    got = _table()
    assert got[(1, "fee")]["value"] == got[(2, "fee")]["value"] == "$60.00"
    rej = _rej(got[(1, "fee")])
    assert rej["row"] == 1 and rej["label"] == "table: fee"
    after = _table([rej])
    assert (1, "fee") not in after and after[(1, "code")]["value"] == "D0120"  # the rest of the row stays
    assert after[(2, "fee")]["value"] == "$60.00"  # the same value in another row is another value
    # the same value in the same row and column, from another spot or label (a re-laid-out page), is the same value:
    # the column's type comes from the table (tasks._ftype walks "tables"), so the value is read back and matched
    pages = [(1, LINE_ITEMS)]
    moved = {**got[(1, "fee")], "label": "local model", "span": None}
    moved_far = dict(moved, value="$60.00")
    assert tasks._is_rejected(moved_far, dict(rej, label="table: fee"), pages, ITEMS)
    assert not tasks._is_rejected(dict(moved_far, row=2), rej, pages, ITEMS)
    assert not tasks._is_rejected(dict(moved_far, value="$61.00"), dict(rej, span=[0, 0]), pages, ITEMS)
    assert tasks._ftype(ITEMS, {"group": "items", "field": "fee"}) == "money"


TWO_PERIODS = """Example Print Co
Invoice
Billing Period:   March 1 - March 31, 2030
Total      $40.00
Statement period: April 1 - April 30, 2030
"""


def test_a_rejected_range_goes_on_to_the_next_range():
    got, _ = _extract("invoice", TWO_PERIODS)
    assert got["period_start"]["value"] == "2030-03-01" and got["period_start"]["label"] == "billing period"
    after, _ = _extract("invoice", TWO_PERIODS, [_rej(got["period_start"])])
    assert after["period_start"]["value"] == "2030-04-01" and after["period_start"]["label"] == "statement period"
    assert after["period_end"]["value"] == "2030-03-31"  # the other end of the first range is untouched
    # with no other range, the field takes its next candidate by the usual rules, or is missing; never the rejected
    one = TWO_PERIODS.replace("Statement period: April 1 - April 30, 2030\n", "")
    got, _ = _extract("invoice", one)
    after, _ = _extract("invoice", one, [_rej(got["period_start"])])
    assert after.get("period_start", {}).get("value") != "2030-03-01"


TWO_LINKS = """Example Cloud Co
Invoice
Your bill is ready: https://billing.example.test/view?year=2030&month=3
Last month: https://billing.example.test/view?year=2030&month=2
Total      $40.00
"""


def test_a_rejected_link_month_goes_on_to_the_next_link():
    got, _ = _extract("invoice", TWO_LINKS)
    start = got["period_start"]
    assert (start["value"], start["label"]) == ("2030-03-01", "month in a link")
    assert start["span"]  # the link's own spot, so a rejection names this link only
    after, _ = _extract("invoice", TWO_LINKS, [_rej(start)])
    assert after["period_start"]["value"] == "2030-02-01"
    assert after["period_end"]["value"] == "2030-03-31"  # its own field: untouched


TWO_NAMES = """Example Garden Shop
Sample Plant Nursery
Receipt
Date: 01/09/2030
Total      $5.00
"""


def test_a_rejected_top_of_page_name_goes_on_to_the_next_line():
    got, _ = _extract("receipt", TWO_NAMES)
    v = got["vendor"]
    assert (v["value"], v["label"]) == ("Example Garden Shop", "top of page")  # no known sender on this page
    assert v["span"] == [0, len("Example Garden Shop")]  # the cell's own spot
    after, _ = _extract("receipt", TWO_NAMES, [_rej(v)])
    assert (after["vendor"]["value"], after["vendor"]["label"]) == ("Sample Plant Nursery", "top of page")


def test_value_spot():
    from doc_intake import fields as fx
    assert fx.value_spot("anything", (3, 7), "x") == [3, 7]  # a span comes back as a list
    assert fx.value_spot("Paid to EXAMPLE CO today", None, "Example Co") == [8, 18]  # case-insensitive
    assert fx.value_spot("Paid to Example Co", None, "Ex") is None  # under three characters: too common to place
    assert fx.value_spot("Paid to Example Co", None, "Other Co") is None
    assert fx.value_spot("Paid to Example Co", None, None) is None
