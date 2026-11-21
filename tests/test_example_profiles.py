"""The example profiles under profiles/ (what a new host starts from): each loads and validates, the example
known-senders map parses and its patterns compile, and a fake receipt, a fake invoice and two fake utility bills give
their required fields with the engine pointed at profiles/. Every vendor, domain, number and amount here is made up."""
import json
import os
import re

import pytest

import doc_intake
import host
from doc_intake import fields as fx
from helpers import ACCOUNT, CARD, FAKES

PROFILES = os.path.join(os.path.dirname(host.HERE), "profiles")
SENDERS = os.path.join(PROFILES, "senders.json")
EXAMPLES = ("receipt", "invoice", "utility_bill")


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


def test_example_profiles_drop_detector_values(examples):
    """A card number on the receipt's payment lines and an account number in the invoice-number spot never come
    back: the payment method keeps only its type, and a value overlapping a detected number is dropped and counted
    by kind."""
    receipt = RECEIPT.replace("Payment Method:   Visa", f"Payment Method:   Visa {CARD}\nAmount Charged:   {CARD}.00")
    got, _, dropped = _values("receipt", receipt)
    assert got["payment_method"] == "card"  # pay_method can't hold digits: the card is never a candidate
    assert dropped == {"card": 1}  # "amount charged" outranks "total", so its card-number "amount" is offered
    assert got["total"] == "$22.14"  # and the next label down gives the real total
    invoice = INVOICE.replace("DP-20300401", ACCOUNT)
    got_inv, _, dropped_inv = _values("invoice", invoice)
    assert "invoice_number" not in got_inv
    assert sum(dropped_inv.values()) == 1
    for value in list(got.values()) + list(got_inv.values()):
        assert not any(fake in value for fake in FAKES), value


# Two fake utility bills from made-up providers on .test domains, in the two layouts bills come in: labels with their
# values on the same line, and a row of labels with the values on the line below. Each carries an account number and
# a service address the profile must never return.
UTILITY_SAME_LINE = f"""EXAMPLE POWER & LIGHT
billing@example-power.test

Account Number:    {ACCOUNT}
Service Address:   100 Sample Lane, Exampleton
Statement Date:    11/05/2030
Service Period:    10/03/2030 - 11/02/2030
Due Date:          11/26/2030

Previous Balance                 $84.20
Payment Received - Thank You     $84.20
Current Charges                  $91.37
Total Amount Due                 $91.37
"""

UTILITY_BELOW = f"""Sample Water District
customer care: sample-water.test

Account Number          Bill Date          Due Date
{ACCOUNT}             12/01/2030         12/22/2030

Service Address
200 Example Road, Sampleville

Service From            Service To
10/28/2030              11/27/2030

Previous Balance        Payments           Amount Due
$40.10                  $40.10             $38.65
"""

UTILITY_NEVER = (ACCOUNT, "Sample Lane", "Example Road", "100 ", "200 ")


@pytest.mark.parametrize("text, want", [
    (UTILITY_SAME_LINE, {"provider": "Example Power & Light", "statement_date": "2030-11-05",
                         "period_start": "2030-10-03", "period_end": "2030-11-02", "due_date": "2030-11-26",
                         "amount_due": "$91.37", "previous_balance": "$84.20", "payments_received": "$84.20"}),
    (UTILITY_BELOW, {"provider": "Sample Water District", "statement_date": "2030-12-01",
                     "period_start": "2030-10-28", "period_end": "2030-11-27", "due_date": "2030-12-22",
                     "amount_due": "$38.65", "previous_balance": "$40.10", "payments_received": "$40.10"}),
], ids=["same-line", "below"])
def test_fake_utility_bills_give_their_fields(examples, text, want):
    got, prof, dropped = _values("utility_bill", text)
    required = {f["name"] for f in prof["fields"] if f.get("required")}
    assert required <= set(got)
    assert got == want  # every field, exactly, and nothing else
    assert not dropped
    for value in got.values():
        assert not any(bit in value for bit in UTILITY_NEVER), value


def test_utility_bill_never_takes_account_or_address(examples):
    """No field can hold an account number, service address, customer name or meter number, and an account number
    printed where a field's value goes is dropped and counted, never returned."""
    prof = fx.load_profile("utility_bill")
    names = {f["name"] for f in prof["fields"]}
    for word in ("account", "address", "customer", "meter", "name"):
        assert not any(word in n for n in names), word
    assert all(f["type"] in ("date", "money", "org_name") for f in prof["fields"])
    # an account number where the payments figure goes (a misread row): "money" can't hold it whole, and the
    # detector drops any piece that overlaps it
    text = UTILITY_SAME_LINE.replace("Payment Received - Thank You     $84.20",
                                     f"Payment Received - Thank You     {ACCOUNT}.00")
    got, _, dropped = _values("utility_bill", text)
    assert "payments_received" not in got
    assert sum(dropped.values()) == 1
    for value in got.values():
        assert not any(fake in value for fake in FAKES), value


def test_example_routes_send_utility_bills_to_their_profile():
    """examples/doc-intake.toml: both fake bills route to the utility_bill profile, and the fake invoice
    still routes to its own (the fake receipt never says "receipt", so it matches no rule)."""
    import tomllib
    from doc_intake import cli
    with open(os.path.join(os.path.dirname(PROFILES), "examples", "doc-intake.toml"), "rb") as f:
        rules = cli._rules(tomllib.load(f)["route"])
    for text, profile in ((UTILITY_SAME_LINE, "utility_bill"), (UTILITY_BELOW, "utility_bill"),
                          (INVOICE, "invoice")):
        assert cli.classify(rules, text)["profile"] == profile
    assert cli.classify(rules, RECEIPT) is None
