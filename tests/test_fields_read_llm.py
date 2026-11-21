"""Field profiles, approval and read, the access log, cleanup, extracts, the local model pass, near-duplicates,
model-made titles and guessed dates, through the fake host (tests/host.py) against a throwaway records root only.

The local model is a mock server on 127.0.0.1: no test talks to a real model. Every value is fake (see helpers.py).
"""
import json
import os
import selectors
import socket
import subprocess
import sys
import time

import pytest

import host
from doc_intake import fields as fx, llm, places, reader, similar, tasks
from doc_intake.config import CFG
from doc_intake.places import DocError
from doc_intake.scan import DATE_NOTE
from helpers import ACCOUNT, CARD, EIN, FAKES, ROUTING, SSN, drop, mode, rec, run  # noqa: F401  rec is a fixture

INBOX = host.INBOX
STATE = host.STATE
PROFILES = fx.profile_names()  # the known-senders map beside them is never listed



def existing(root, relp, text):
    """A file already in the records (filed before intake)."""
    p = root / relp
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---------------------------------------------------------------- profiles
SAMPLE = {"money": "$1,250.00", "percent": "80%", "cost": "$25 copay", "limit": "$300,000", "date": "01/15/2030",
          "duration": "6 months", "eligibility": "children only", "basis": "UCR", "money_or_none": "$640",
          "plan_type": "PPO", "w2_box12": "D 1,500.00", "form_1099": "1099-NEC", "currency": "USD",
          "pay_method": "VISA", "policy_type": "homeowners", "ref": "INV-2030-17", "cdt_code": "D2740",
          "tooth_area": "8", "short_text": "Crown porcelain", "org_name": "Example Supply Co",
          "plan_name": "Example PPO Plan", "year": "2030", "count": "60", "weight_lbs": "6,100 lbs"}
SENSITIVE_LINES = [f"SSN {SSN}", f"Card {CARD}", f"Routing {ROUTING}", f"Account number {ACCOUNT}",
                   "Member ID: XYZ123456789", "Employee ID 00123456", f"EIN {EIN}", "someone@example.com",
                   "Date of birth 01/02/1980", "Policy number PX99887766", "Loan number 7766554433"]


def kitchen_sink(prof):
    lines = [f"{f['labels'][0] if isinstance(f['labels'][0], str) else f['labels'][0][0]}   "
             f"{SAMPLE[f['type']]}" for f in prof["fields"] if f.get("labels")]
    lines += ["Shoe size   $9.00", "Favorite color   blue", "Secret plan   $1.00"]
    return "\n".join(SENSITIVE_LINES[:5] + lines + SENSITIVE_LINES[5:]) + "\n"


@pytest.mark.parametrize("name", PROFILES)
def test_profile_allowlist_and_no_sensitive_output(name):
    prof = fx.load_profile(name)
    allowed = fx.allowed_fields(prof)
    records, dropped, _ = fx.extract(prof, [(1, kitchen_sink(prof))], "x.txt")
    assert records, f"{name}: nothing extracted from its own labels"
    for r in records:
        assert (r["group"], r["field"]) in allowed
        assert set(r) == {"group", "plan", "row", "field", "value", "file", "page", "label"}
    out = json.dumps(records)
    for v in FAKES + ["PX99887766", "7766554433", "01/02/1980", "1980-01-02", "Shoe", "blue"]:
        assert v not in out, f"{name} leaked {v}"


def test_offer_refuses_a_field_outside_the_profile():
    prof = fx.load_profile("receipt")
    ex = fx.Extraction(prof)
    ex.offer("receipt", None, None, "card_number", "money", "$1.00", 1, "x", "f")
    ex.offer("other", None, None, "total", "money", "$1.00", 1, "x", "f")
    assert ex.records() == []


def test_sensitive_value_dropped_and_counted():
    prof = fx.load_profile("invoice")
    records, dropped, _ = fx.extract(prof, [(1, f"Invoice #   {CARD}\nAmount due   $10.00\n")], "x")
    assert "invoice_number" not in {r["field"] for r in records}
    assert dropped.get("card") == 1 and CARD not in json.dumps(records)


@pytest.mark.parametrize("name", PROFILES)
def test_unrelated_document_gives_no_fields(name):
    text = "A short note.\nNothing here but words and more words.\nThe end.\n"
    records, _, _ = fx.extract(fx.load_profile(name), [(1, text)], "x")
    assert records == []


BENEFITS = """2030 Open Enrollment Benefits Guide
Medical Plans            Example PPO Plan      Example HDHP Plan
Deductible (individual / family)   $500 / $1,000    $1,700 / $3,400
Employee only    $45.00    $20.00
Dental coverage and vision coverage, HSA and FSA options.
"""


def test_benefits_plans_and_columns():
    text = BENEFITS + "Primary care visit   $25 copay   20% after deductible\nHealth care FSA   $3,300\n"
    records, _, plans = fx.extract(fx.load_profile("benefits"), [(1, text)], "x")
    v = {(r["plan"], r["field"]): r["value"] for r in records}
    assert plans["medical"] == ["Example PPO Plan", "Example HDHP Plan"]
    assert v[("Example PPO Plan", "deductible_individual")] == "$500"
    assert v[("Example HDHP Plan", "deductible_family")] == "$3,400"
    assert v[("Example HDHP Plan", "plan_type")] == "HDHP"
    assert v[("Example HDHP Plan", "primary_care_copay")] == "20% after deductible"
    assert v[(None, "health_fsa_limit")] == "$3,300"


def test_treatment_estimate_table():
    text = ("Treatment Plan Estimate\n"
            "Code     Tooth   Description              Fee        Ins Est    Pt Est\n"
            "D2740    8       Crown porcelain          $1,200.00  $600.00    $600.00\n"
            "D0120            Periodic exam            $60.00     $60.00     $0.00\n"
            "\n\nPatient name John Example   Member ID: XYZ123456789\n")
    records, _, _ = fx.extract(fx.load_profile("treatment_estimate"), [(1, text)], "x")
    rows = {}
    for r in records:
        rows.setdefault(r["row"], {})[r["field"]] = r["value"]
    assert rows[1] == {"cdt_code": "D2740", "tooth_area": "8", "procedure": "Crown porcelain", "fee": "$1,200.00",
                       "insurance_estimate": "$600.00", "patient_estimate": "$600.00"}
    assert "tooth_area" not in rows[2] and rows[2]["fee"] == "$60.00"
    assert "John" not in json.dumps(records)


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


def test_paystub_allowlist_and_bank_numbers(rec, capsys):
    drop(rec, "stub.txt", PAYSTUB)
    assert run(["fields", "--task", "paystub", f"{INBOX}/stub.txt"]) == 0
    out = capsys.readouterr().out
    (t,) = (rec / STATE / "tasks").iterdir()
    raw = t.read_text()
    task = json.loads(raw)
    v = {r["field"]: r["value"] for r in task["values"]}
    assert v["gross_pay_current"] == "2,000.00" and v["net_pay_ytd"] == "1,372.00"
    assert v["pay_date"] == "2030-01-15" and v["period_end"] == "2030-01-14"
    assert v["pretax_401k_403b_current"] == "100.00" and v["posttax_roth_401k_current"] == "50.00"
    assert v["pretax_medical_current"] == "45.00" and v["state_income_tax_current"] == "81.00"
    allowed = fx.allowed_fields(fx.load_profile("paystub"))
    assert all((r["group"], r["field"]) in allowed for r in task["values"])
    assert not any("rate" in r["field"] or "hours" in r["field"] for r in task["values"])
    for bad in (ROUTING, ACCOUNT, "8901", SSN, "00123456", "25.00", "80.00"):
        assert bad not in raw and bad not in out


# ---------------------------------------------------------------- approval, read, access log, cleanup
def _task(rec):
    drop(rec, "stub.txt", PAYSTUB)
    run(["fields", "--task", "paystub", f"{INBOX}/stub.txt"])
    (t,) = (rec / STATE / "tasks").iterdir()
    return t


def test_read_needs_approval_and_logs(rec, capsys):
    t = _task(rec)
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "check net pay"]) == 2
    assert "not approved" in capsys.readouterr().err
    assert run(["approve", t.name]) == 0
    assert run(["read", t.name, "--reason", "check net pay", "--fields", "net_pay_current"]) == 0
    out = capsys.readouterr().out
    body = json.loads(out[out.index("{"):])
    assert [r["field"] for r in body["values"]] == ["net_pay_current"]
    log = rec / STATE / "access.log"
    assert mode(log) == 0o600
    lines = log.read_text().splitlines()
    reads = [ln.split("\t") for ln in lines if ln.split("\t")[1] == "read"]
    assert len(reads) == 1
    ts, action, target, what, reason = reads[0]
    assert target == f"{STATE}/tasks/{t.name}" and what == "fields=pay.net_pay_current"
    assert reason == "reason=check net pay" and ts[:4].isdigit()


def test_read_needs_reason(rec):
    t = _task(rec)
    with pytest.raises(SystemExit):
        run(["read", t.name])


def test_preview_only_in_a_terminal(rec, monkeypatch, capsys):
    t = _task(rec)
    out = capsys.readouterr().out
    assert "1,372.00" not in out and "preview" in out
    assert run(["preview", t.name]) == 2
    monkeypatch.setattr(places, "is_tty", lambda: True)
    assert run(["preview", t.name]) == 0
    assert "1,372.00" in capsys.readouterr().out


def test_extract_requires_pages(rec):
    drop(rec, "a.txt", "hello")
    with pytest.raises(SystemExit):
        run(["extract", f"{INBOX}/a.txt"])
    with pytest.raises(DocError):
        tasks.parse_pages("")
    assert tasks.parse_pages("1-3,5") == [1, 2, 3, 5]


def test_extract_redacts_named_pages_and_gates_read(rec, capsys):
    drop(rec, "multi.txt", f"page one SSN {SSN}\fpage two card {CARD}\fpage three secret-free")
    assert run(["extract", f"{INBOX}/multi.txt", "--pages", "1-2"]) == 0
    (ex,) = (rec / STATE / "extracts").iterdir()
    body = ex.read_text()
    assert mode(ex) == 0o600 and ex.name.endswith(" - multi p1-2.txt")
    assert SSN not in body and CARD not in body and "page three" not in body
    assert f"# source: {INBOX}/multi.txt" in body and "# redacted: ssn 1, card 1" in body
    assert "pattern-based and can miss things" in body and "# needs-review: no" in body
    assert run(["read", ex.name, "--reason", "r"]) == 2
    run(["approve", ex.name])
    capsys.readouterr()
    assert run(["read", ex.name, "--reason", "user asked about page 2"]) == 0
    assert "[CARD-last4:1111]" in capsys.readouterr().out
    log = (rec / STATE / "access.log").read_text()
    assert f"\tread\t{STATE}/extracts/" in log and "pages=1-2" in log


def test_cleanup(rec, capsys):
    tdir = rec / STATE / "tasks"
    tdir.mkdir(parents=True)
    old = {"task": "paystub", "created": "2000-01-01T00:00:00", "keep": False, "approved": True}
    (tdir / "paystub-old.json").write_text(json.dumps(old))
    (tdir / "paystub-kept.json").write_text(json.dumps({**old, "keep": True}))
    (tdir / "benefits-old.json").write_text(json.dumps({**old, "task": "benefits"}))
    new = {**old, "created": "2999-01-01T00:00:00"}
    (tdir / "paystub-new.json").write_text(json.dumps(new))
    ex = rec / STATE / "extracts"
    ex.mkdir()
    (ex / "a.txt").write_text("# keep: no\n# ----\nx")
    os.utime(ex / "a.txt", (0, 0))
    run(["cleanup", "--task", "paystub", "--dry-run"])
    assert (tdir / "paystub-old.json").exists()
    run(["cleanup", "--task", "paystub"])
    assert not (tdir / "paystub-old.json").exists()
    assert (tdir / "paystub-kept.json").exists() and (tdir / "benefits-old.json").exists()
    assert (tdir / "paystub-new.json").exists() and (ex / "a.txt").exists()
    run(["cleanup"])
    assert not (tdir / "benefits-old.json").exists() and not (ex / "a.txt").exists()
    assert (tdir / "paystub-new.json").exists()
    run(["keep", "paystub-new.json"])
    run(["cleanup", "--older-than", "0"])
    assert (tdir / "paystub-new.json").exists() and (tdir / "paystub-kept.json").exists()


# ---------------------------------------------------------------- the local model pass (mock server)
FAKE_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_model.py")
STARTUP_SECONDS = 10


class FakeModel:
    """The test side of tests/fake_model.py: setting reply/raw rewrites the config file the server reads on every
    request, and seen reads back the request bodies it recorded."""

    def __init__(self, workdir):
        self._config = workdir / "config.json"
        self._seen = workdir / "seen.jsonl"
        self._reply, self._raw = {"spans": []}, None
        self._write()
        self._seen.write_text("")

    def _write(self):
        self._config.write_text(json.dumps({"reply": self._reply, "raw": self._raw}))

    @property
    def reply(self):
        return self._reply

    @reply.setter
    def reply(self, value):
        self._reply = value
        self._write()

    @property
    def raw(self):
        return self._raw

    @raw.setter
    def raw(self, value):
        self._raw = value
        self._write()

    @property
    def seen(self):
        return [json.loads(line) for line in self._seen.read_text().splitlines() if line]


def _wait_for_port(proc, deadline):
    """The port the server prints once it listens, then a connect to prove it accepts; bounded by deadline."""
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        if not sel.select(timeout=max(0.0, deadline - time.monotonic())):
            raise RuntimeError("fake model server printed no port in time")
    finally:
        sel.close()
    line = proc.stdout.readline()
    if not line.strip().isdigit():
        raise RuntimeError(f"fake model server failed to start (exit {proc.poll()})")
    port = int(line)
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return port
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("fake model server never accepted connections")
            time.sleep(0.05)


@pytest.fixture
def ollama(monkeypatch, tmp_path):
    """A fake model server in its own process (tests/fake_model.py) on 127.0.0.1 and a free port; the engine is
    pointed at it. Its own process, not a thread here, so the reader's fork() never runs in a threaded process."""
    workdir = tmp_path / "fake-model"
    workdir.mkdir()
    fake = FakeModel(workdir)
    proc = subprocess.Popen([sys.executable, "-I", FAKE_MODEL, str(workdir / "config.json"),
                             str(workdir / "seen.jsonl")], stdout=subprocess.PIPE, text=True)
    try:
        monkeypatch.setattr(llm, "OLLAMA_PORT", _wait_for_port(proc, time.monotonic() + STARTUP_SECONDS))
        yield fake
    finally:
        proc.kill()
        proc.wait(timeout=STARTUP_SECONDS)
        proc.stdout.close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_llm_not_reachable_falls_back(rec, monkeypatch, capsys):
    monkeypatch.setattr(llm, "OLLAMA_PORT", _free_port())
    drop(rec, "a.txt", f"Dear Jane Example, SSN {SSN}")
    assert run(["extract", f"{INBOX}/a.txt", "--pages", "1", "--llm-check", "--model", "m"]) == 0
    assert CFG.start_hint in capsys.readouterr().out
    (ex,) = (rec / STATE / "extracts").iterdir()
    body = ex.read_text()
    assert SSN not in body and "# llm-check: skipped (Ollama not reachable)" in body
    assert "# needs-review: no" in body


def test_llm_flags_replace_spans_and_need_review(rec, ollama):
    ollama.reply = {"spans": [{"text": "Jane Example", "kind": "name"}, {"text": "1 Example Street", "kind": "address"},
                              {"text": "SSN", "kind": "name"}, {"text": "not in the text", "kind": "name"}]}
    drop(rec, "a.txt", f"Dear Jane Example of 1 Example Street, SSN {SSN}. Jane Example again.")
    run(["extract", f"{INBOX}/a.txt", "--pages", "1", "--llm-check", "--model", "m"])
    (ex,) = (rec / STATE / "extracts").iterdir()
    body = ex.read_text()
    assert "Jane Example" not in body and "1 Example Street" not in body and SSN not in body
    assert body.count("[LLM-FLAGGED:name]") == 2 and "[LLM-FLAGGED:address]" in body and "[SSN]" in body
    assert "# llm-flags: name 2, address 1" in body or "# llm-flags: address 1, name 2" in body
    assert "# needs-review: yes" in body
    sent = ollama.seen[0]
    assert sent["options"]["num_ctx"] == llm.NUM_CTX and SSN not in sent["prompt"] and "[SSN]" in sent["prompt"]


def test_llm_malformed_json_needs_review(rec, ollama):
    ollama.raw = "this is not json {"
    drop(rec, "a.txt", "plain words")
    assert run(["extract", f"{INBOX}/a.txt", "--pages", "1", "--llm-check", "--model", "m"]) == 0
    (ex,) = (rec / STATE / "extracts").iterdir()
    body = ex.read_text()
    assert "# llm-check: failed 1 of 1 chunks" in body and "# needs-review: yes" in body
    assert "plain words" in body


def test_llm_only_talks_to_localhost(monkeypatch):
    monkeypatch.setattr(llm, "OLLAMA_HOST", "example.com")
    with pytest.raises(llm.LLMError):
        llm._url("/api/tags")


def test_llm_chunks_fit():
    text = ("x" * 100 + "\n") * 200
    parts = llm.chunks(text)
    assert "".join(parts) == text and all(len(p) <= llm.CHUNK_CHARS for p in parts)


def test_llm_fill_is_validated(rec, ollama):
    ollama.reply = {"values": [
        {"group": "medical", "plan": "Example PPO Plan", "field": "er_copay", "value": "$250 copay"},
        {"group": "medical", "plan": "Example PPO Plan", "field": "urgent_care_copay", "value": "call us"},
        {"group": "medical", "plan": "Made Up Plan", "field": "specialist_copay", "value": "$50"},
        {"group": "medical", "plan": "Example PPO Plan", "field": "member_id", "value": "XYZ123456789"},
        {"group": "medical", "plan": "Example HDHP Plan", "field": "specialist_copay", "value": SSN},
    ]}
    drop(rec, "b.txt", BENEFITS)
    run(["fields", "--task", "benefits", f"{INBOX}/b.txt", "--llm", "--model", "m"])
    (t,) = (rec / STATE / "tasks").iterdir()
    task = json.loads(t.read_text())
    got = [(r["plan"], r["field"], r["value"]) for r in task["values"] if r["label"] == "local model"]
    assert got == [("Example PPO Plan", "er_copay", "$250 copay")]
    assert SSN not in t.read_text() and "XYZ123456789" not in t.read_text()


# ---------------------------------------------------------------- near-duplicates, model titles, guessed dates
_WORDS = ("garden shed plan notes about the north fence the gate hinge oil the paint colors the compost bin "
          "and the hose reel with spring tasks summer tasks and autumn tasks for the yard and the porch steps "
          "plus the gutter cleaning list the window screens the lawn edge and the mulch order for the beds").split()
MANUAL = " ".join(_WORDS * 3)
MANUAL_2 = MANUAL.replace("hinge", "latch", 1)          # one word changed: a re-download or an edited copy
OTHER = " ".join(reversed(("kitchen drawer inventory with spoons forks knives cups plates bowls jars lids towels "
                           "sponges soap racks trays mats boards pans pots kettles timers scales").split() * 4))
OLD = "Housing/old/manual.txt"


def test_fingerprint_similarity_near_and_far():
    a, b, c = similar.fingerprint(MANUAL), similar.fingerprint(MANUAL_2), similar.fingerprint(OTHER)
    assert similar.jaccard(a["minhash"], a["minhash"]) == 1.0
    assert similar.is_near(a, b) is not None and similar.is_near(a, b) >= similar.JACCARD_MIN
    assert similar.is_near(a, c) is None and similar.similarity(a, c)[0] < 0.2
    assert similar.fingerprint("too short to compare") is None
    assert len(a["simhash"]) == 16 and len(a["minhash"]) <= similar.MINHASH_K


def test_records_cache_holds_no_text(rec, capsys):
    existing(rec, OLD, MANUAL + " zebrafinch quartzlamp")
    drop(rec, "manual v2.txt", MANUAL_2 + " zebrafinch quartzlamp")
    run(["scan"])
    for f in (rec / STATE).iterdir():
        if f.is_file():
            body = f.read_text().lower()
            assert "zebrafinch" not in body and "quartzlamp" not in body and "compost" not in body
    entry = json.loads((rec / STATE / "records-hashes.json").read_text())["files"][OLD]
    assert set(entry) == {"size", "mtime_ns", "sha256", "text_fp"} and set(entry["text_fp"]) == {"simhash", "minhash"}
    assert "zebrafinch" not in capsys.readouterr().out


def test_possible_duplicate_in_scan(rec, capsys):
    existing(rec, OLD, MANUAL)
    drop(rec, "manual v2.txt", MANUAL_2)
    drop(rec, "drawer.txt", OTHER)
    run(["scan"])
    out = capsys.readouterr().out
    assert f"possible duplicate of {OLD} (similarity " in out
    assert out.count("possible duplicate of") == 1 and "1 possible duplicate" in out
    assert "duplicate of Housing" not in out.replace("possible duplicate of Housing", "")


def test_possible_duplicate_within_a_batch(rec, capsys):
    drop(rec, "a.txt", MANUAL)
    drop(rec, "b.txt", MANUAL_2)
    run(["scan"])
    assert f"possible duplicate of {INBOX}/a.txt (similarity " in capsys.readouterr().out


NOTES = "Dear Jane Example,\n" + MANUAL + "\n"


def _titled(rec, capsys, argv):
    drop(rec, "notes.txt", NOTES)
    run(["scan"] + argv)
    return capsys.readouterr().out


def test_llm_names_uses_a_checked_title(rec, ollama, capsys):
    ollama.reply = {"title": "Yard work plan"}
    out = _titled(rec, capsys, ["--llm-names", "--model", "m"])
    assert " - Yard work plan.txt" in out and "name from the local model: check it" in out
    sent = ollama.seen[-1]
    assert sent["options"]["num_ctx"] == llm.NUM_CTX
    titles = rec / STATE / "titles.json"
    assert mode(titles) == 0o600 and "Yard work plan" in titles.read_text()
    # a later scan without --llm-names reuses the cached title without asking the model again
    n = len(ollama.seen)
    run(["scan"])
    assert " - Yard work plan.txt" in capsys.readouterr().out and len(ollama.seen) == n


@pytest.mark.parametrize("reply", [
    {"title": "Jane Example yard plan"},          # a name from the text
    {"title": "Yard plan 1234567"},                # a number longer than 4 digits
    {"title": "Plan"},                             # too short
    {"title": "a very long title with far too many words"},
    {"title": f"SSN {SSN} plan"},
    {"nothing": "here"},
])
def test_llm_names_rejects_unsafe_titles(rec, ollama, capsys, reply):
    ollama.reply = reply
    out = _titled(rec, capsys, ["--llm-names", "--model", "m"])
    assert " - notes.txt" in out and "name from the local model" not in out
    assert not (rec / STATE / "titles.json").exists()


def test_llm_names_rejects_private_terms(rec, ollama, capsys):
    (term,) = host.privacy_terms()
    ollama.reply = {"title": f"{term.title()} yard plan"}
    assert " - notes.txt" in _titled(rec, capsys, ["--llm-names", "--model", "m"])


def test_llm_names_malformed_reply_falls_back(rec, ollama, capsys):
    ollama.raw = "not json {"
    assert " - notes.txt" in _titled(rec, capsys, ["--llm-names", "--model", "m"])


def test_llm_names_ollama_down_or_no_model(rec, monkeypatch, capsys):
    monkeypatch.setattr(llm, "OLLAMA_PORT", _free_port())
    out = _titled(rec, capsys, ["--llm-names", "--model", "m"])
    assert f"{llm.not_reachable()}; names from file names" in out and " - notes.txt" in out
    out = _titled(rec, capsys, ["--llm-names"])
    assert "no model" in out and " - notes.txt" in out
    out = _titled(rec, capsys, [])
    assert "--llm-names" not in out and " - notes.txt" in out


def test_guessed_date_is_flagged(rec, capsys):
    drop(rec, "undated.txt", "a letter about nothing in particular\n")
    drop(rec, "dated.txt", "a letter written 02/03/2030 about nothing\n")
    run(["scan"])
    out = capsys.readouterr().out
    assert out.count(DATE_NOTE) == 1
    rows = [line for line in out.splitlines() if line.startswith("  ") and DATE_NOTE in line]
    assert rows and rows[0].startswith("  2:")  # undated.txt sorts second


def test_labeled_date_beats_the_first_date(rec):
    text = "Printed 01/02/2030\nAccount summary\nStatement date: 03/04/2030\n"
    p = drop(rec, "s.txt", text)
    assert host.route(str(p), "text", text)["name"].startswith("2030-03-04 - ")
    text = "Notice dated 01/02/2030. Date of service 02/05/2030."
    assert host.route(str(p), "text", text)["name"].startswith("2030-02-05 - ")
