"""The review page, approval status, superseded task files and the invoice/receipt field fixes (fake data only)."""
import datetime as dt
import json
import pathlib

import host
import pytest
from helpers import CARD, ROUTING, SSN, drop, mode, run

from doc_intake import detect, places, preview, reader, taskfiles
from doc_intake import fields as fx
from doc_intake.config import CFG
from doc_intake.dates import document_date
from doc_intake.places import DocError
from doc_intake.tasks import approved_task_for

IN = host.INBOX



@pytest.fixture
def rec(records, monkeypatch):
    monkeypatch.setattr(places, "is_tty", lambda: False)
    monkeypatch.setattr(reader, "_zxing", lambda: None)
    monkeypatch.setattr(preview, "has_display", lambda: False)
    return records


CLOUD_TEXT = """                                                                              Example Cloud Services Invoice
                                                                          Invoice Summary
                                                                          Invoice Number:          123456789
                                                                          Invoice Date:            November 2 , 2030

                                                                          TOTAL AMOUNT DUE ON November 2 , 2030      $12.34

     This invoice is for the billing period October 1 - October 31 , 2030
          Greetings from Example Cloud Services, we're writing to provide you with an electronic invoice for the charges shown on this
"""

STRIPE_TEXT = """Invoice
Invoice number      ABCD1234-0007
Date of issue       March 3, 2030
Date due            March 3, 2030

Example Cloud Co    Bill to
                    Someone

$20.00 due March 3, 2030

Description                     Qty   Unit price    Amount
Pro plan                        1     $20.00        $20.00
Total                                               $20.00
Amount due                                          $20.00 USD
"""

YEAR_TEXT = """Example Hosting Co
Invoice Date:  Nov 1, 2030
Amount due     $5.00
"""


def ext(profile, text):
    recs, dropped, _ = fx.extract(fx.load_profile(profile), [(1, text)], "x")
    return {r["field"]: r["value"] for r in recs}, {r["field"]: r["label"] for r in recs}, dropped


# ---------------------------------------------------------------- vendors, trimming, invoice numbers, dates
def test_senders_json_is_not_a_profile():
    assert "senders" not in fx.profile_names()
    assert "invoice" in fx.profile_names()


def test_senders_map_refused_as_a_profile(rec, capsys):
    drop(rec, "a.txt", CLOUD_TEXT)
    with pytest.raises(fx.ProfileError, match="known-senders map, not a profile"):
        fx.load_profile("senders")
    assert run(["fields", "--task", "senders", f"{IN}/a.txt"]) == 2
    err = capsys.readouterr().err
    assert "known-senders map, not a profile" in err and "invoice" in err
    tasks = rec / ".doc-intake" / "tasks"
    assert not tasks.exists() or not list(tasks.iterdir())


def test_senders_map_under_any_name_is_not_a_profile(tmp_path, monkeypatch):
    """A map with a third name (neither vendors.json nor senders.json) is skipped because senders_file names it."""
    (tmp_path / "invoice.json").write_text(pathlib.Path(host.PROFILES, "invoice.json").read_text(encoding="utf-8"))
    (tmp_path / "known_billers.json").write_text(json.dumps({"vendors": [{"name": "Example Cloud"}]}))
    monkeypatch.setattr(CFG, "profiles_dir", str(tmp_path))
    monkeypatch.setattr(CFG, "senders_file", str(tmp_path / "known_billers.json"))
    assert fx.profile_names() == ["invoice"]
    with pytest.raises(fx.ProfileError, match="not a profile"):
        fx.load_profile("known_billers")
    monkeypatch.chdir(tmp_path)  # a relative senders_file names the same file
    monkeypatch.setattr(CFG, "senders_file", "known_billers.json")
    assert fx.profile_names() == ["invoice"]
    with pytest.raises(fx.ProfileError, match="not a profile"):
        fx.load_profile("known_billers")
    elsewhere = tmp_path.parent / (tmp_path.name + "-maps")
    elsewhere.mkdir()
    (elsewhere / "link.json").symlink_to(tmp_path / "known_billers.json")  # senders_file is a link to it
    monkeypatch.setattr(CFG, "senders_file", str(elsewhere / "link.json"))
    assert fx.profile_names() == ["invoice"]
    with pytest.raises(fx.ProfileError, match="not a profile"):
        fx.load_profile("known_billers")
    (elsewhere / "real.json").write_text(json.dumps({"vendors": []}))  # the profiles folder holds a link to it
    (tmp_path / "linked_map.json").symlink_to(elsewhere / "real.json")
    monkeypatch.setattr(CFG, "senders_file", str(elsewhere / "real.json"))
    assert fx.profile_names() == ["invoice", "known_billers"]
    with pytest.raises(fx.ProfileError, match="not a profile"):
        fx.load_profile("linked_map")
    (tmp_path / "linked_map.json").unlink()
    monkeypatch.setattr(CFG, "senders_file", None)  # no map configured: the file is just another JSON file
    assert fx.profile_names() == ["invoice", "known_billers"]
    (tmp_path / "vendors.json").write_text(json.dumps({"vendors": []}))  # the fallback name, never listed
    assert fx.profile_names() == ["invoice", "known_billers"]


def test_senders_map_order():
    names = [v[0] for v in fx.load_vendors()[0]]
    assert names.index("Example Cloud Marketplace") < names.index("Example Cloud")
    for want in ("Sample Software", "Demo Print Shop", "Example Water Utility", "Example Motor Credit"):
        assert want in names


@pytest.mark.parametrize("text,name", [
    ("Example Cloud Services Invoice\nsomething", "Example Cloud"),
    ("Example Cloud Marketplace\nInvoice for a plan\nExample Cloud Services, Inc.", "Example Cloud Marketplace"),
    ("Receipt from Sample Software, PBC\nAmount paid $5.00", "Sample Software"),
    ("Questions? Contact billing@sample-software.test", "Sample Software"),
    ("See your bill at example-cloud.test/billing", "Example Cloud"),
    ("Sent by billing@marketplace.example-cloud.test", "Example Cloud Marketplace"),
    ("Demo Print Shop order summary", "Demo Print Shop"),
    ("Example Water bill\nAmount due $30.00", "Example Water Utility"),
])
def test_known_vendor_map(text, name):
    assert fx.known_vendor([(1, text)])[0] == name


def test_marketplace_only_from_the_header():
    filler = "\n".join(f"line {i}" for i in range(30))
    kv = fx.known_vendor([(1, "Example Cloud Services Invoice\n" + filler + "\nExample Cloud Marketplace charges\n")])
    assert kv[0] == "Example Cloud"


def test_cloud_invoice_fields():
    v, labels, _ = ext("invoice", CLOUD_TEXT)
    assert v["vendor"] == "Example Cloud" and labels["vendor"] == "known sender"
    assert v["invoice_date"] == "2030-11-02"
    assert v["period_start"] == "2030-10-01" and v["period_end"] == "2030-10-31"
    assert v["invoice_number"] == "123456789"
    assert v["total"] == "$12.34"


def test_from_inside_a_sentence_is_not_a_vendor_label():
    text = CLOUD_TEXT.replace("Example Cloud Services", "Example Provider")
    v, labels, _ = ext("invoice", text)
    assert "charges" not in v.get("vendor", "") and labels.get("vendor") != "from"


def test_stripe_from_name_trimmed_at_bill_to():
    v, labels, _ = ext("invoice", STRIPE_TEXT)
    assert v["vendor"] == "Example Cloud Co" and labels["vendor"] == "stripe: from name"
    assert v["invoice_number"] == "ABCD1234-0007"
    assert v["invoice_date"] == "2030-03-03"


@pytest.mark.parametrize("raw,clean", [
    ("Example Co, PBC Bill to", "Example Co, PBC"),
    ("Example Co   Invoice number", "Example Co"),
    ("Example Co\nSomewhere", "Example Co"),
    ("Example Co Ship to", "Example Co"),
    ("Example Co Page 1 of 2", "Example Co"),
    ("Example Co", "Example Co"),
])
def test_trim_vendor(raw, clean):
    assert fx.trim_vendor(raw) == clean


@pytest.mark.parametrize("v,ok", [("2026", False), ("2030", False), ("123", False), ("2030-11-01", False),
                                  ("ABCD-0001", True), ("INV12", True), ("123456", True), ("12-34", True),
                                  ("12345", False)])
def test_invoice_number_rule(v, ok):
    assert fx.invoice_number_ok(v) is ok


def test_invoice_number_never_the_year():
    v, _, _ = ext("invoice", YEAR_TEXT)
    assert "invoice_number" not in v
    assert v["invoice_date"] == "2030-11-01"


@pytest.mark.parametrize("s,iso", [("November 2 , 2030", "2030-11-02"), ("Nov 2, 2030", "2030-11-02"),
                                   ("2030-11-02", "2030-11-02")])
def test_date_spacing(s, iso):
    assert fx.parse_date(s) == iso
    assert fx._typed("date", f"Invoice Date: {s}")


@pytest.mark.parametrize("text,start,end", [
    ("Billing Period: October 1 - October 31, 2030", "2030-10-01", "2030-10-31"),
    ("Billing period December 1 - January 31 , 2031", "2030-12-01", "2031-01-31"),
    ("Service period Mar 1, 2030 to Mar 31, 2030", "2030-03-01", "2030-03-31"),
])
def test_date_ranges(text, start, end):
    v, _, _ = ext("invoice", "Example Co\nInvoice number INV-1\n" + text + "\nTotal  $1.00\n")
    assert (v["period_start"], v["period_end"]) == (start, end)


# ---------------------------------------------------------------- suspicious flags
TODAY = dt.date(2030, 6, 1)


@pytest.mark.parametrize("field,ftype,value,flagged", [
    ("vendor", "org_name", "the charges shown on this", True),
    ("vendor", "org_name", "Example Co Bill to", True),
    ("vendor", "org_name", "A" * 41, True),
    ("vendor", "org_name", "Page", True),
    ("vendor", "org_name", "Example Co", False),
    ("invoice_number", "ref", "2026", True),
    ("invoice_number", "ref", "INV-7", False),
    ("invoice_date", "date", "1999-12-31", True),
    ("invoice_date", "date", "2030-09-01", True),
    ("invoice_date", "date", "2030-07-15", False),
    ("total", "money", "$0.00", True),
    ("total", "money", "-5.00", True),
    ("total", "money", "$5.00", False),
])
def test_suspicious_values(field, ftype, value, flagged):
    assert bool(fx.suspicious(field, ftype, value, TODAY)) is flagged


def test_currency_mismatch():
    recs = [{"field": "total", "type": "money", "value": "€5.00"}, {"field": "currency", "type": "currency",
                                                                     "value": "USD"}]
    assert 1 in fx.document_flags(recs, TODAY)
    recs[0]["value"] = "$5.00"
    assert fx.document_flags(recs, TODAY) == {}


# ---------------------------------------------------------------- the review page, status, approve, supersede
def _invoice_task(rec, *names):
    names = names or ("a.txt",)
    for n in names:
        drop(rec, n, STRIPE_TEXT if n != "bad.txt" else "Invoice Date:  Nov 1, 2030\nAmount due     $5.00\n")
    assert run(["fields", "--task", "invoice"] + [f"{IN}/{n}" for n in names]) == 0
    return sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]


def test_html_preview_is_local_and_private(rec, capsys):
    drop(rec, "leaky.txt", STRIPE_TEXT + f"\nSSN {SSN}\nCard {CARD}\nContact someone@example.com\n")
    t = _invoice_task(rec, "a.txt", "bad.txt")
    run(["fields", "--task", "invoice", f"{IN}/leaky.txt"])
    t2 = sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]
    capsys.readouterr()
    for task in (t, t2):
        assert run(["preview", "--html", task.name]) == 0  # works without a terminal: it writes a file
        out = capsys.readouterr().out
        assert "$20.00" not in out and "Example Cloud Co" not in out  # counts only on stdout
        page = rec / ".doc-intake" / "previews" / (task.stem + ".html")
        assert mode(page) == 0o600 and mode(page.parent) == 0o700
        html = page.read_text()
        assert "<script" not in html.lower() and "http" not in html.lower() and "src=" not in html.lower()
        assert "@import" not in html and "url(" not in html
        assert detect.find_spans(html) == []
        assert SSN not in html and CARD not in html
        assert f'{host.CLI} approve &quot;{task.name}&quot;' in html
        assert "prefers-color-scheme: dark" in html
    html = (rec / ".doc-intake" / "previews" / (t.stem + ".html")).read_text()
    assert "Example Cloud Co" in html and "a.txt" in html and "bad.txt" in html
    assert 'class="miss"' in html  # bad.txt has no vendor: a required field, red
    assert "<b>2</b>documents" in html and "<b>1</b>with every needed field" in html


def test_cli_preview_still_needs_a_terminal(rec, capsys):
    t = _invoice_task(rec)
    assert run(["preview", t.name]) == 2
    assert "own terminal" in capsys.readouterr().err


def test_html_preview_opens_only_with_a_display(rec, monkeypatch, capsys):
    t = _invoice_task(rec)
    opened = []
    monkeypatch.setattr(preview.subprocess, "Popen", lambda argv, **kw: opened.append(argv))
    run(["preview", "--html", t.name])
    assert not opened
    monkeypatch.setattr(preview, "has_display", lambda: True)
    run(["preview", "--html", "--no-open", t.name])
    assert not opened
    run(["preview", "--html", t.name])
    assert opened and opened[0][0] == "xdg-open" and opened[0][1].endswith(t.stem + ".html")


def test_approve_says_what_it_approved(rec, capsys):
    t = _invoice_task(rec, "a.txt", "b.txt")
    capsys.readouterr()
    assert run(["approve", t.name]) == 0
    out = capsys.readouterr().out.strip()
    n = len(json.loads(t.read_text())["values"])
    assert out == f"approved: {t.name} (2 documents, {n} fields)"


def test_status_lists_counts_and_no_values(rec, capsys):
    t = _invoice_task(rec, "a.txt", "bad.txt")
    capsys.readouterr()
    assert run(["status"]) == 0
    out = capsys.readouterr().out
    assert t.name in out and "approved" in out.splitlines()[0]
    row = next(ln for ln in out.splitlines() if ln.startswith(t.name)).split()
    assert row[1:4] == ["invoice", "no", "2"]
    run(["approve", t.name])
    capsys.readouterr()
    run(["status", "--detail"])
    out = capsys.readouterr().out
    assert next(ln for ln in out.splitlines() if ln.startswith(t.name)).split()[2] == "yes"
    assert "vendor" in out and "matched by" in out
    for v in json.loads(t.read_text())["values"]:
        if v["field"] not in ("currency",):
            assert v["value"] not in out, v["field"]


def test_status_with_no_tasks(rec, capsys):
    assert run(["status"]) == 0
    assert "no task files" in capsys.readouterr().out


def test_replacing_an_unapproved_task_overwrites_it(rec, capsys):
    t = _invoice_task(rec)
    assert run(["fields", "--task", "invoice", "--replaces", t.name, f"{IN}/a.txt"]) == 0
    assert "replaced" in capsys.readouterr().out
    assert [p.name for p in (rec / ".doc-intake" / "tasks").iterdir()] == [t.name]


def test_superseded_approved_task_is_never_read(rec, capsys):
    t = _invoice_task(rec)
    run(["approve", t.name])
    assert run(["fields", "--task", "invoice", "--replaces", t.name, f"{IN}/a.txt"]) == 0
    new = t.with_name(t.stem + "-v2.json")
    assert new.exists() and json.loads(new.read_text())["replaces"] == t.name
    old = json.loads(t.read_text())
    assert old["superseded_by"] == new.name and old["approved"] is True
    capsys.readouterr()
    assert run(["read", t.name, "--reason", "test"]) == 2
    assert "superseded" in capsys.readouterr().err
    assert run(["approve", t.name]) == 2
    assert run(["fields", "--task", "invoice", "--replaces", t.name, f"{IN}/a.txt"]) == 2  # already superseded
    assert run(["fields", "--task", "receipt", "--replaces", new.name, f"{IN}/a.txt"]) == 2  # wrong profile
    assert run(["fields", "--task", "invoice", "--replaces", new.name, f"{IN}/a.txt"]) == 0  # re-run the -v2
    assert json.loads(new.read_text())["replaces"] == t.name
    run(["status"])
    out = capsys.readouterr().out
    assert f"superseded by {new.name}" in out and f"replaces {t.name}" in out
    # file never finds a superseded task, even approved
    digest = json.loads(new.read_text())["sources"][0]["sha256"]
    assert approved_task_for(digest) is None
    run(["approve", new.name])
    assert approved_task_for(digest)[0].endswith(new.name)


def test_labels_debug_prints_no_values(rec, capsys):
    drop(rec, "a.txt", STRIPE_TEXT + f"\nCard {CARD}\n")
    capsys.readouterr()
    assert run(["fields", "--task", "invoice", "--labels", f"{IN}/a.txt"]) == 0
    out = capsys.readouterr().out
    assert "invoice_number <- \"invoice number\"" in out and "detector card" in out
    for v in ("ABCD1234-0007", "$20.00", "Example Cloud Co", CARD, "March"):
        assert v not in out
    assert not (rec / ".doc-intake" / "tasks").exists() or not any((rec / ".doc-intake" / "tasks").iterdir())


# ---------------------------------------------------------------- email receipts and the routing detector
def eml(received_id=ROUTING):
    return ("Delivered-To: someone@example.com\r\n"
            f"Received: by 2002:a05:1234 with SMTP id {received_id};\r\n"
            "        Fri, 14 Nov 2030 10:00:00 -0800\r\n"
            "From: Example Parking <receipts@example.com>\r\n"
            "Subject: Here's your parking receipt\r\n"
            "Date: Fri, 14 Nov 2030 10:00:00 -0800\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            "<html><head><style>p{color:red}</style></head><body><table><tr><td>Total</td><td>$12.00</td></tr>"
            "<tr><td>Payment method</td><td>Visa</td></tr></table></body></html>\r\n")


def test_routing_ignores_mail_transport_headers():
    assert "routing" not in detect.counts(detect.find_spans(eml()))
    body = "Bank routing number " + ROUTING
    assert "routing" in detect.counts(detect.find_spans(body))
    # a header block that isn't transport still counts
    assert "routing" in detect.counts(detect.find_spans("Subject: pay " + ROUTING + "\r\n\r\nhello"))


def test_email_receipt_fields(rec):
    p = drop(rec, "parking.eml", eml())
    doc = reader.read_document(str(p))
    assert doc["method"] == "email"
    assert "routing" not in reader.sensitive_counts(doc)
    rc = run(["fields", "--task", "receipt", f"{IN}/parking.eml"])
    assert rc == 0
    t = sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]
    v = {r["field"]: r["value"] for r in json.loads(t.read_text())["values"]}
    assert v["vendor"] == "Example Parking" and v["total"] == "$12.00" and v["date"] == "2030-11-14"
    assert v["payment_method"] == "card"


# ---------------------------------------------------------------- event registrations, renewal dates, phone, address
RACE_TEXT = """                         2030 Example City Marathon
Registration Date: 03/04/2030
Registration ID: 123456789
Participant
Phone: 804-555-0123
Address:
123 Example Ln
Exampleville, VA 23220
Event Cost:     $95.00
Payment Method: Visa
"""

RACE_NO_AMOUNT = """                         2030 Example City Half Marathon
Registration Date: 03/04/2030
Registration ID: 123456789
You're registered! Show this confirmation at packet pickup.
"""

PLAN_TEXT = """Example Mobile
Success! Your plan is renewed.
Plan renews on:      Oct 12, 2031
Plan expires:        Oct 12, 2031
Payment date:        Oct 13, 2030
Total                $45.00
"""


def test_race_registration_is_a_receipt():
    v, labels, dropped = ext("receipt", RACE_TEXT)
    assert v["vendor"] == "Example City Marathon" and labels["vendor"] == "event heading"
    assert v["total"] == "$95.00" and labels["total"] == "event cost"
    assert v["date"] == "2030-03-04"
    assert "123456789" not in v.values()  # Registration ID: the receipt profile has no reference field
    assert not any("555" in x or "Example Ln" in x for x in v.values())


def test_registration_without_an_amount_is_marked(rec, capsys):
    drop(rec, "race.txt", RACE_NO_AMOUNT)
    assert run(["fields", "--task", "receipt", f"{IN}/race.txt"]) == 0
    t = sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]
    run(["preview", "--html", t.name])
    html = (rec / ".doc-intake" / "previews" / (t.stem + ".html")).read_text()
    assert preview.NO_AMOUNT in html


def test_payment_date_beats_renewal_and_expiry(monkeypatch):
    monkeypatch.setattr(fx.dt, "date", _FixedDate)
    v, labels, _ = ext("receipt", PLAN_TEXT)
    assert v["date"] == "2030-10-13" and labels["date"] == "payment date"
    assert document_date(PLAN_TEXT) == "2030-10-13"
    # no label: the first date that is neither a renewal date nor far ahead
    assert document_date("Renews Oct 12, 2031\nCharged Oct 13, 2030\n") == "2030-10-13"
    # a near date after renewal words still beats a far-future "next payment date"
    assert document_date("Your plan renewed on Oct 13, 2030\nNext payment date: Oct 12, 2031\n") == "2030-10-13"
    # only a far-future date: taken, since there is no other (and the preview flags it)
    assert document_date("Next bill Oct 12, 2031\n") == "2031-10-12"


class _FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2030, 10, 20)


@pytest.mark.parametrize("text,kind", [
    ("Phone: 804-555-0123", "phone"),
    ("call (804) 555-0123 today", "phone"),
    ("+1 804.555.0123", "phone"),
    ("1-804-555-0123", "phone"),
    ("Tel: 8045550123", "phone"),
    ("Mobile 804 555 0123", "phone"),
    ("Address: 123 Example Ln, Exampleville, VA", "address"),
    ("Street: 9 Example Way", "address"),
    ("Ship to\n123 Example Ln\nExampleville, VA 23220", "address"),
    ("45 North Example Ave Apt 2, Exampleville, VA", "address"),
])
def test_phone_and_address_detected(text, kind):
    red, counts = detect.redact(text)
    assert counts.get(kind) == 1, counts
    assert f"[{kind.upper()}]" in red and "555" not in red and "Example Ln" not in red


@pytest.mark.parametrize("text", ["order 8045550123", "2030-11-20", "Total 123 456 7890 items", "12:30 pm",
                                  "Email address: someone", "Invoice 2030-0001", "Qty 3  Unit 555-1234",
                                  "IP address: 10.0.0.1", "1 Example Item, size L"])
def test_phone_and_address_not_on_plain_text(text):
    counts = detect.redact(text)[1]
    assert "phone" not in counts and "address" not in counts, counts


def test_phone_and_address_dropped_from_fields_and_counted_in_scan(rec, capsys):
    drop(rec, "race.txt", RACE_TEXT)
    run(["scan"])
    out = capsys.readouterr().out
    assert "phone 1" in out and "address 1" in out and "555-0123" not in out
    run(["fields", "--task", "receipt", f"{IN}/race.txt"])
    t = sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]
    body = t.read_text()
    assert "555-0123" not in body and "Example Ln" not in body and "Exampleville" not in body


def test_extract_redacts_phone_and_address(rec, capsys):
    drop(rec, "race.txt", RACE_TEXT)
    assert run(["extract", f"{IN}/race.txt", "--pages", "1"]) == 0
    (x,) = (rec / ".doc-intake" / "extracts").iterdir()
    text = x.read_text()
    assert "[PHONE]" in text and "[ADDRESS]" in text and "555-0123" not in text and "Example Ln" not in text


RACE_LABELS_FIRST = """                         Example Person
                Gender:  M
       Age on Race Day:  30
                 Email:  someone@example.com
                 Phone:  804-555-0123
2030 Example City Marathon
             Event Cost:  $95.00
      Registration ID:  123456789
Registration Date: March 4, 2030 @ 3:00pm
"""


def test_race_vendor_skips_label_lines_and_prefers_event_names():
    # "Age on Race Day:" matches the weak word "race" but is a field label, not the event's heading
    v, labels, _ = ext("receipt", RACE_LABELS_FIRST)
    assert v["vendor"] == "Example City Marathon" and labels["vendor"] == "event heading"


# ---------------------------------------------------------------- "bill is ready" notices: month in a link, file-name date
NOTICE = ("Example Cloud\nYour billing statement is available.\nTotal  $12.34\n"
          "You can see a complete break down of all charges here:\n"
          "https://console.example.com/billing/home#/bills?year=2030&month=9\n")


def test_link_month_gives_the_period():
    v, lab, _ = ext("invoice", NOTICE)
    assert (v["period_start"], v["period_end"]) == ("2030-09-01", "2030-09-30")
    assert lab["period_start"] == "month in a link"


@pytest.mark.parametrize("q,start,end", [("month=12&year=2030", "2030-12-01", "2030-12-31"),
                                         ("year=2032&month=2", "2032-02-01", "2032-02-29"),
                                         ("year=2030&month=13", None, None)])
def test_link_month_forms(q, start, end):
    v, _, _ = ext("invoice", NOTICE.replace("year=2030&month=9", q))
    assert (v.get("period_start"), v.get("period_end")) == (start, end)


def test_labeled_period_beats_the_link():
    v, _, _ = ext("invoice", NOTICE + "Billing Period: October 1 - October 31, 2030\n")
    assert (v["period_start"], v["period_end"]) == ("2030-10-01", "2030-10-31")


def test_file_name_date_only_when_the_text_has_none():
    prof = fx.load_profile("invoice")
    recs, _, _ = fx.extract(prof, [(1, NOTICE)], f"{IN}/2030-10-01 - example.com - Statement.pdf")
    got = {r["field"]: (r["value"], r["label"]) for r in recs}
    assert got["invoice_date"] == ("2030-10-01", "file name")
    recs, _, _ = fx.extract(prof, [(1, NOTICE + "Invoice Date: October 2, 2030\n")],
                            f"{IN}/2030-10-01 - example.com - Statement.pdf")
    assert {r["field"]: r["value"] for r in recs}["invoice_date"] == "2030-10-02"
    recs, _, _ = fx.extract(prof, [(1, NOTICE)], f"{IN}/statement.pdf")
    assert "invoice_date" not in {r["field"] for r in recs}


@pytest.mark.parametrize("s,want", [("$54.5548 per day", "$54.5548"), ("$436.44", "$436.44"), ("$1,234.50", "$1,234.50"),
                                    ("12.50", "12.50")])
def test_money_keeps_rate_decimals(s, want):
    assert fx._typed("money", s)[0][2] == want


def test_labels_output_hides_an_unknown_header_word(rec, monkeypatch, capsys):
    """A page that starts like a mail header block: a detector hit under a "Word:" line that isn't a standard header
    shows as "in mail header other", never the word (it could be a name) and never the value."""
    page = ("From: shop@example.com\nFakename: 4111111111111111\nSubject: Invoice INV-1\n\n"
            "Invoice number INV-1\nAmount due $5.00\n")
    drop(rec, "mail.txt", "placeholder\n")
    monkeypatch.setattr(reader, "read_bounded_or_fail",
                        lambda p, *a, **kw: {"pages": [(1, page)], "total": 1, "method": "text", "codes": []})
    assert run(["fields", "--task", "invoice", "--labels", "inbox/mail.txt"]) == 0
    out, err = capsys.readouterr()
    assert "detector card" in out and "in mail header other" in out
    assert "Fakename" not in out + err and "4111111111111111" not in out + err


def test_resolve_output_refuses_a_file_outside_the_state_folder(rec):
    (rec / "loose.json").write_text("{}")
    with pytest.raises(DocError, match="no task file or extract"):
        taskfiles.resolve_output(str(rec / "loose.json"))
