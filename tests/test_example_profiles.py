"""The example profiles under profiles/ (what a new host starts from): each loads and validates, the example
known-senders map parses and its patterns compile, and a fake receipt and a fake invoice give their required fields
with the engine pointed at profiles/. Every vendor, domain, number and amount here is made up."""
import json
import os
import re

import pytest

import doc_intake
import host
from doc_intake import fields as fx

PROFILES = os.path.join(os.path.dirname(host.HERE), "profiles")
SENDERS = os.path.join(PROFILES, "senders.json")
EXAMPLES = ("receipt", "invoice")


@pytest.fixture
def examples():
    """The engine pointed at the example profiles and senders map; the fake host's settings put back after."""
    doc_intake.configure(profiles_dir=PROFILES, senders_file=SENDERS)
    yield
    host.configure()


def test_every_example_profile_loads(examples):
    names = sorted(n[:-5] for n in os.listdir(PROFILES) if n.endswith(".json") and n != "senders.json")
    assert set(EXAMPLES) <= set(names)
    for name in names:
        prof = fx.load_profile(name)
        assert prof["name"] == name  # the task name comes from "name", the file from the profile's name
        assert prof["description"] and prof["never"]
        assert any(f.get("required") for f in prof["fields"])


def test_example_profiles_use_no_host_words():
    for name in EXAMPLES:
        with open(os.path.join(PROFILES, name + ".json"), encoding="utf-8") as f:
            text = f.read().lower()
        for word in ("book", "rental", "ledger", "schedule c", "deduct", "category"):
            assert word not in text, (name, word)


def test_senders_map_parses_and_compiles(examples):
    with open(SENDERS, encoding="utf-8") as f:
        spec = json.load(f)
    assert spec["description"]
    for v in spec["vendors"]:
        assert v["name"]
        assert all(d.endswith(".test") for d in v.get("domains", []))
        for p in v.get("patterns", []):
            re.compile(p, re.I)
        assert set(v) <= {"name", "domains", "patterns", "header_lines", "kind", "capture"}
    assert any("header_lines" in v for v in spec["vendors"])
    assert any("kind" in v for v in spec["vendors"])
    for p in spec["stripe"]["markers"] + spec["events"]["markers"] + [spec["stripe"]["bill_to"],
                                                                      spec["events"]["heading"]]:
        re.compile(p, re.I)
    vendors, stripe, _, _ = fx.load_vendors()  # the engine's own reader
    assert len(vendors) == len(spec["vendors"]) and stripe


RECEIPT = """SAMPLE HARDWARE STORE
Store 0042 · sample-hardware.test

Transaction Date:   03/14/2030
Item A                         $12.00
Item B                          $8.50
Subtotal                       $20.50
Sales Tax                       $1.64
Total                          $22.14
Payment Method:   Visa
Thank you for shopping with us
"""

INVOICE = """Demo Print Shop                                        INVOICE
billing@demo-print.test

Invoice Number:   DP-20300401
Invoice Date:     04/01/2030
Billing Period:   March 1 - March 31, 2030

Description                                   Amount
Business cards (500)                         $45.00
Subtotal                                     $45.00
Tax                                           $3.60
Amount Due                                   $48.60
Currency:   USD
"""


def _values(name, text):
    prof = fx.load_profile(name)
    recs, dropped, _ = fx.extract(prof, [(1, text)], "fake.txt")
    return {r["field"]: r["value"] for r in recs}, prof, dropped


def test_fake_receipt_gives_its_required_fields(examples):
    got, prof, dropped = _values("receipt", RECEIPT)
    required = {f["name"] for f in prof["fields"] if f.get("required")}
    assert required <= set(got)
    assert got["date"] == "2030-03-14"
    assert got["vendor"] == "Sample Hardware Store"  # from the senders map
    assert got["total"] == "$22.14"
    assert got["tax"] == "$1.64"
    assert got["payment_method"] == "card"
    assert not dropped


def test_fake_invoice_gives_its_required_fields(examples):
    got, prof, dropped = _values("invoice", INVOICE)
    required = {f["name"] for f in prof["fields"] if f.get("required")}
    assert required <= set(got)
    assert got["vendor"] == "Demo Print Shop"
    assert got["invoice_date"] == "2030-04-01"
    assert got["period_start"] == "2030-03-01"
    assert got["period_end"] == "2030-03-31"
    assert got["total"] == "$48.60"
    assert got["tax"] == "$3.60"
    assert got["currency"] == "USD"
    assert got["invoice_number"] == "DP-20300401"
    assert not dropped


def test_lender_kind_only_matches_a_kind_field(examples):
    page = [(1, "Example Motor Credit\nStatement Date  05/01/2030\n")]
    assert fx.known_vendor(page) == ("Example Motor Credit", 1, "known sender")
    assert fx.known_vendor(page, "lender") == ("Example Motor Credit", 1, "known sender")
    assert fx.known_vendor([(1, "Demo Print Shop\n")], "lender") is None
