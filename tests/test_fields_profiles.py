"""Field extraction on fake text with the example profiles under tests/fixtures/profiles/: a Closing Disclosure
(closing_disclosure), a vehicle sale contract (vehicle_purchase) and lender statements (auto_loan_statement), plus
the VIN and contract-number detectors. Laid out like pdftotext -layout output; every name, address, number, lender
and dealer here is made up.

The vehicle value types are a host's (fields.register_type), so the vehicle_types fixture registers them the way a
host would and puts the engine's type tables back afterwards; the vehicle_purchase profile that needs them lives in
its own folder (tests/fixtures/profiles-vehicle/), so tests that load every shared profile never meet those types. Routing these papers to folders is a host's job and
is not tested here."""
import json
import os
import re

import pytest

from doc_intake import detect, fields as fx
from doc_intake.config import CFG
from helpers import CARD, ROUTING, SSN

# ---------------------------------------------------------------- closing_disclosure
LOAN_ID = "9876543210"
NAMES = ["Pat Example", "Sam Example", "Lee Sample", "Example Title Co", "Example Lender"]
ADDRESS = "100 Example Lane"
NMLS = "NMLS 555123"

# Columns on page 2: borrower at closing, borrower before closing, seller at closing, seller before, others.
COLS = (78, 90, 104, 116, 128)


def row(label, *vals):
    """A Closing Cost Details line: label, then each value right-aligned to end at its column + 10."""
    line = label
    for col, v in zip(COLS, vals):
        if v:
            line = line.ljust(col + 10 - len(v)) + v
    return line


PAGE1 = f"""CLOSING DISCLOSURE                                                         PAGE 1 OF 5 • LOAN ID # {LOAN_ID}
Closing Information                Transaction Information                 Loan Information
Date Issued     4/10/2030          Borrower   {NAMES[0]} and {NAMES[1]}        Loan Term     30 years
Closing Date    4/15/2030                     {ADDRESS}                    Purpose       Purchase
Disbursement Date 4/15/2030        Seller     {NAMES[2]}                      Product       Fixed Rate
Settlement Agent {NAMES[3]}        Lender     {NAMES[4]}                   Loan ID #     {LOAN_ID}
Sale Price      $180,000                                                   MIC #         000654321

 Loan Terms                                                  Can this amount increase after closing?
 Loan Amount                       $162,000                  NO
 Interest Rate                     3.875 %                   NO

   Mortgage Insurance              +              82.35
 Cash to Close                     $14,147.26           Includes Closing Costs. See Calculating Cash to Close on page 3 for details.
"""

PAGE2 = "\n".join([
    f"CLOSING DISCLOSURE                                                         PAGE 2 OF 5 • LOAN ID # {LOAN_ID}",
    "Closing Cost Details",
    " " * 82 + "Borrower-Paid" + " " * 17 + "Seller-Paid           Paid by",
    " Loan Costs" + " " * 117 + "Others",
    " " * 77 + "At Closing Before Closing   At Closing Before Closing",
    row("A. Origination Charges", "$1,802.00"),
    row("01 0.25 % of Loan Amount (Points)", "$405.00"),
    row("02 Application Fee", "$300.00"),
    row("B. Services Borrower Did Not Shop For", "$236.55"),
    row("02 Credit Report Fee                to Example Credit Bureau", "", "$29.80"),
    row("C. Services Borrower Did Shop For", "$2,655.50"),
    row("03 Title – Insurance Binder         to Example Title Co.", "$650.00"),
    row("04 Title – Lender’s Title Insurance to Example Title Co.", "$500.00"),
    row("05 Title - Settlement Agent Fee     to Example Title Co.", "$400.00"),
    " Other Costs",
    row("E. Taxes and Other Government Fees", "$205.00"),
    row("01 Recording Fees                      Deed: $40.00      Mortgage: $45.00", "$85.00"),
    row("02 Transfer Tax                        to Any State", "", "", "$950.00"),
    row("03 Transfer Tax                        to Any County", "$120.00"),
    row("F. Prepaids", "$2,120.80"),
    row("01 Homeowner’s Insurance Premium ( 12 mo.) to Example Insurance Co.", "$1,209.96"),
    "02 Mortgage Insurance Premium ( mo.)",
    row("03 Prepaid Interest ( $17.44 per day from 4/15/30 to 5/1/30 )", "$279.04"),
    row("04 Property Taxes ( 6 mo.) to Any County", "$631.80"),
    row("G. Initial Escrow Payment at Closing", "$412.25"),
    row("01 Homeowner’s Insurance $100.83 per month for 2 mo.", "$201.66"),
    "02 Mortgage Insurance                  per month for mo.",
    row("03 Property Taxes            $105.30 per month for 2 mo.", "$210.60"),
    row("H. Other", "$2,400.00"),
    row("07 Title – Owner’s Title Insurance (optional) to Example Title Co.", "", "", "$1,000.00"),
    row("J. TOTAL CLOSING COSTS (Borrower-Paid)", "$9,712.10", "", "$12,800.00"),
    "",
])

PAGE3 = f"""CLOSING DISCLOSURE                                                         PAGE 3 OF 5 • LOAN ID # {LOAN_ID}
Calculating Cash to Close                      Use this table to see what has changed from your Loan Estimate.
                                                Loan Estimate      Final     Did this change?
Total Closing Costs (J)                         $8,054.00          $9,712.10    YES
Cash to Close                                   $16,054.00         $14,147.26
BORROWER’S TRANSACTION                                                        SELLER’S TRANSACTION
K. Due from Borrower at Closing                    $189,762.30                 M. Due to Seller at Closing      $180,080.00
01 Sale Price of Property                          $180,000.00                 01 Sale Price of Property        $180,000.00
02 Sale Price of Any Personal Property Included in Sale    $500.00             02 Sale Price of Any Personal Property
Adjustments for Items Paid by Seller in Advance                               Adjustments for Items Paid by Seller in Advance
08    City/Town Taxes   1/1/30 to 4/14/30          $80.00                      09 City/Town Taxes               $80.00
09    County Taxes      to
L. Paid Already by or on Behalf of Borrower at Closing                        N. Due from Seller at Closing
02 Loan Amount                                     $162,000.00                 02 Closing Costs Paid at Closing (J) $12,800.00
Adjustments for Items Unpaid by Seller                                        Adjustments for Items Unpaid by Seller
12     City/Town Taxes  1/1/30 to 4/14/30          $365.04                     14    City/Town Taxes            $365.04
13     County Taxes     1/1/30 to 4/14/30          $210.75                     15    County Taxes               $210.75
"""


def test_closing_disclosure_fields_from_a_fake_cd():
    prof = fx.load_profile("closing_disclosure")
    records, dropped, _ = fx.extract(prof, [(1, PAGE1), (2, PAGE2), (3, PAGE3)], "cd.pdf")
    single = {r["field"]: r["value"] for r in records if r["row"] is None}
    assert single == {
        "closing_date": "2030-04-15",
        "sale_price": "$180,000.00",
        "loan_amount": "$162,000",
        "loan_term": "30 years",
        "interest_rate": "3.875 %",
        "origination_charges": "$1,802.00",
        "points_pct": "0.25 %",
        "points_amount": "$405.00",
        "recording_fees": "$85.00",
        "prepaid_homeowners_insurance_months": "12 mo",
        "prepaid_homeowners_insurance": "$1,209.96",
        "prepaid_interest_per_day": "$17.44",
        "prepaid_interest_total": "$279.04",
        "prepaid_property_taxes_months": "6 mo",
        "prepaid_property_taxes": "$631.80",
        "escrow_homeowners_insurance_monthly": "$100.83",
        "escrow_homeowners_insurance": "$201.66",
        "escrow_property_taxes_monthly": "$105.30",
        "escrow_property_taxes": "$210.60",
        "total_closing_costs": "$9,712.10",
        "cash_to_close": "$14,147.26",
        "seller_credit_city_town_taxes": "$365.04",
        "seller_credit_county_taxes": "$210.75",
        "paid_by_seller_in_advance_city_town_taxes": "$80.00",
    }
    # Several title and transfer tax lines, one row each, borrower-paid only: the seller-paid owner's policy and the
    # seller-paid state transfer tax are left out.
    rows = sorted((r["field"], r["row"], r["value"]) for r in records if r["row"] is not None)
    assert rows == [("title_charges", 1, "$650.00"), ("title_charges", 2, "$500.00"),
                    ("title_charges", 3, "$400.00"), ("transfer_taxes", 1, "$120.00")]
    # No mortgage insurance on this loan: its blank lines give nothing.
    assert not {"prepaid_mortgage_insurance", "escrow_mortgage_insurance"} & set(single)
    out = json.dumps(records)
    for bad in [LOAN_ID, ADDRESS, NMLS, "000654321", "$12,800.00", "$950.00", "$1,000.00"] + NAMES:
        assert bad not in out, bad


def test_en_dash_label_does_not_match_a_payee():
    prof = {"name": "t", "default_group": "g", "groups": {"g": {"global": True}},
            "fields": [{"group": "g", "name": "title", "type": "money", "labels": ["title –"], "each": True}]}
    fx.check_profile(prof)
    recs, _, _ = fx.extract(prof, [(1, "01 Survey Fee     to Example Title Co.     $85.00\n"
                                      "02 Title – Search   to Example Title Co.     $90.00\n")], "x")
    assert [r["value"] for r in recs] == ["$90.00"]


# ---------------------------------------------------------------- vehicle types (a host registers them)
MAKES = ("Subaru", "Ford", "Honda", "Land Rover")
MAKE = "(?:" + "|".join(re.escape(m).replace(r"\ ", r"\s") for m in MAKES) + ")"


@pytest.fixture
def vehicle_types(monkeypatch):
    """Register vehicle_make and vehicle the way a host does, and restore the engine's type tables after."""
    for name in ("TYPES", "_RX", "_FULL"):
        monkeypatch.setattr(fx, name, dict(getattr(fx, name)))
    for name in ("_CI", "TITLE_CASE"):
        monkeypatch.setattr(fx, name, set(getattr(fx, name)))
    monkeypatch.setattr(fx, "MODEL_PREFIX", fx.MODEL_PREFIX)
    fx.register_type("vehicle_make", MAKE, ci=True, title_case=True, model_prefix=True)
    # make, then one to three words of up to 12 characters (never a 17-character VIN printed after them)
    fx.register_type("vehicle", MAKE + r"(?: (?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9-]{0,11}"
                     r"(?![A-Za-z0-9-])){1,3}", ci=True, title_case=True)


VEHICLE_PROFILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "profiles-vehicle")


@pytest.fixture
def vehicle(vehicle_types, monkeypatch):
    """The vehicle types, and the engine pointed at the folder holding vehicle_purchase (restored after)."""
    monkeypatch.setattr(CFG, "profiles_dir", VEHICLE_PROFILES)


def make_vin(stem):
    """A VIN with a valid check digit: stem has '?' in position 9."""
    for c in "0123456789X":
        v = stem.replace("?", c)
        if detect.vin_ok(v):
            return v
    raise AssertionError("no check digit fits")


VIN = make_vin("1FTER4FH?MLA12345")
VIN2 = make_vin("2T1BURHE?MC123456")
CONTRACT_NO = "RX-449120"
DEAL_NO = "D77812"
ACCOUNT = "70401234567"
VNAMES = ["Pat Example", "Sam Example"]
PHONE = "(555) 555-0142"
DEALER = "Example Ford of Testville, LLC"

CONTRACT = f"""RETAIL INSTALLMENT SALE CONTRACT - SIMPLE FINANCE CHARGE              Contract No. {CONTRACT_NO}   Deal # {DEAL_NO}
Buyer Name and Address                  Co-Buyer Name and Address           Seller-Creditor (Name and Address)
{VNAMES[0]}                             {VNAMES[1]}                         {DEALER}
{ADDRESS}                                                               1 Example Plaza
Testville, ZZ 00000                                                         Testville, ZZ 00000
Phone {PHONE}
Contract Date  07/11/2031

New/Used   Year Make Model                Vehicle Identification Number      Primary Use For Which Purchased
NEW        2031 FORD RANGER  XLT          {VIN}                  Business
GVWR 6,050 lbs

ANNUAL PERCENTAGE RATE    FINANCE CHARGE      Amount Financed       Total of Payments     Total Sale Price
The cost of your credit   The dollar amount   The amount of credit  The amount you will   The total cost of your
as a yearly rate.         the credit will     provided to you or    have paid after you   purchase on credit,
                          cost you.           on your behalf.       have made all         including your down
                                                                    payments as           payment of $5,000.00
                                                                    scheduled.
6.49 %                    $8,123.40           $38,000.00            $46,123.40            $51,123.40

Your Payment Schedule Will Be:
Number of Payments     Amount of Payments     When Payments Are Due
72                     $640.60                Monthly beginning 08/15/2031

ITEMIZATION OF AMOUNT FINANCED
1 Cash Price (including $ 1,890.00 sales tax)                                       $ 40,890.00
2 Total Downpayment = Gross Trade-In $ 0.00 - Pay Off $ 0.00 + Cash $ 5,000.00       $ 5,000.00
3 Unpaid Balance of Cash Price (1 minus 2)                                          $ 35,890.00
4 Other Charges Including Amounts Paid to Others on Your Behalf
  C Optional Gap Contract                                                           $ 795.00
  F Government License and/or Registration Fees
    Registration Fee                                                                $ 50.75
    Title Fees                                                                      $ 15.00
  H Other Charges to {DEALER} for Dealer Processing Fee      $ 899.00
    to Example Warranty Co. for Service Contract                                    $ 1,350.00
5 Amount Financed (3 + 4)                                                           $ 38,000.00
Account Number: {ACCOUNT}
"""

LENDER_STATEMENT = f"""Statement Date 03/05/2031                           Account Number {ACCOUNT}
Example Motor Credit
Vehicle
2031 HONDA CIVIC LX  {VIN2}
Past Due Payment Amount            $0.00
Current Payment Due                $412.33
Total Amount Due                   $412.33
Payment Due Date                   03/28/2031

{VNAMES[0]}
{ADDRESS}
Testville, ZZ 00000

Regular Payment Amount   $412.33
Date        Transaction            Amount
02/27/2031  Payment - Thank You    $412.33
Maturity Date
07/28/2036
Outstanding Balance   $21,544.10
Payoff Amount: call us or visit example-motor-credit.test for a payoff quote.
"""

GENERIC_STATEMENT = f"""Example Auto Loan Servicing                     Loan Number {CONTRACT_NO}
Auto Loan Statement
Statement Period  January 1 - January 31, 2031
Monthly Payment                    $500.00
                       Paid This Period      Paid Year to Date
Principal              $410.00               $410.00
Interest               $90.00                $90.00
Principal Balance                  $18,250.00
Payoff Amount                      $18,301.12
VIN {VIN}
"""


def ext(profile, text):
    recs, dropped, _ = fx.extract(fx.load_profile(profile), [(1, text)], "x.pdf")
    return {r["field"]: r["value"] for r in recs if r["row"] is None}, recs, dropped


# ---------------------------------------------------------------- detectors
def test_vin_check_digit():
    assert detect.vin_ok("1M8GDM9AXKP042788")  # the check-digit example in 49 CFR 565.15 (X)
    assert detect.vin_ok(VIN) and detect.vin_ok(VIN.lower())
    bad = VIN[:8] + ("1" if VIN[8] != "1" else "2") + VIN[9:]
    assert not detect.vin_ok(bad)
    assert not detect.vin_ok(VIN[:16])  # 16 characters
    assert not detect.vin_ok("1M8GDM9AXKP04278I")  # I, O and Q are never in a VIN


def test_vin_is_counted_and_redacted():
    red, counts = detect.redact(f"Vehicle {VIN} sold")
    assert VIN not in red and "[VIN]" in red and counts == {"vin": 1}
    bad = VIN[:8] + ("1" if VIN[8] != "1" else "2") + VIN[9:]
    assert detect.redact(f"stock {bad} here")[1] == {}  # unlabeled and failing the check: not a VIN
    red, counts = detect.redact(f"VIN: {bad}")  # after a VIN label, any VIN-shaped token (an OCR misread)
    assert bad not in red and counts == {"vin": 1}


def test_contract_and_deal_numbers_are_detected():
    red, counts = detect.redact(f"Contract No. {CONTRACT_NO}   Deal # {DEAL_NO}")
    assert CONTRACT_NO not in red and DEAL_NO not in red and counts == {"contract-id": 2}
    assert detect.redact("Contract Date 07/11/2031")[1] == {}


# ---------------------------------------------------------------- vehicle_purchase
def test_vehicle_purchase_fields_from_a_fake_contract(vehicle):
    got, recs, dropped = ext("vehicle_purchase", CONTRACT)
    assert got == {
        "contract_date": "2031-07-11",
        "year": "2031",
        "make": "Ford",
        "make_model": "Ford Ranger",
        "model": "Ranger",
        "gvwr_lbs": "6050",
        "apr": "6.49 %",
        "finance_charge": "$8,123.40",
        "amount_financed": "$ 38,000.00",  # the itemization's line 5, on its line, before the box
        "total_of_payments": "$46,123.40",
        "total_sale_price": "$51,123.40",
        "term_months": "72",
        "monthly_payment": "$640.60",
        "cash_price": "$ 40,890.00",
        "sales_tax": "$ 1,890.00",
        "down_payment": "$ 5,000.00",
        "optional_gap_contract": "$ 795.00",
        "registration_fees": "$ 50.75",
        "title_fees": "$ 15.00",
        "dealer_fees": "$ 899.00",
        "service_contract": "$ 1,350.00",
        "seller": DEALER,
    }
    out = json.dumps(recs)
    for bad in [VIN, CONTRACT_NO, DEAL_NO, ACCOUNT, ADDRESS, PHONE, "00000"] + VNAMES:
        assert bad not in out, bad


def test_vehicle_purchase_seller_is_never_the_lender(vehicle):
    text = CONTRACT.replace(DEALER, "").replace(
        "Account Number", "Seller assigns its interest in this contract to Example Motor Credit Corporation.\nAccount")
    got, _, _ = ext("vehicle_purchase", text)
    assert got.get("seller") != "Example Motor Credit"


def test_vehicle_type_never_takes_a_vin(vehicle):
    got, _, _ = ext("vehicle_purchase", f"Vehicle Description: 2031 FORD RANGER {VIN}\n")
    assert got["make_model"] == "Ford Ranger" and VIN not in json.dumps(got)
    got, _, _ = ext("vehicle_purchase", f"Year Make Model                VIN\n2031 FORD RANGER   {VIN}\n")
    assert got["make_model"] == "Ford Ranger" and VIN not in json.dumps(got)


def test_contract_date_is_the_signature_date_not_the_first_payment(vehicle):
    # Page 1 holds only the first payment date; the signatures, and the contract date, are on a later page.
    p1 = ("RETAIL INSTALLMENT SALE CONTRACT\nYear Make and Model  2031 Example Truck\n"
          "Number of Payments  Amount of Payments  When Payments Are Due\n"
          "48                  $ 449.51            Monthly beginning 08/25/2031\n")
    p5 = ("You agree to the terms of this contract.\n"
          "i Buyer Signs XF [signed] Date 07/11/2031  Co-Buyer Signs N/A  Date N/A\n"
          "Seller signs EXAMPLE MOTORS Date 07/11/2031 By XF [signed]\n")
    recs, _, _ = fx.extract(fx.load_profile("vehicle_purchase"), [(1, p1), (5, p5)], "x")
    got = {r["field"]: (r["value"], r["label"]) for r in recs}
    assert got["contract_date"] == ("2031-07-11", "buyer signs")


# E-signature stamps on the signature page, laid out the way pdftotext -layout prints them: the stamp ("DocuSigned
# by:", the signer's name, a hex envelope ID, sometimes a timestamp) is taller than one line of text. The buyer
# signed 07/11/2031 and the seller 07/12/2031, so a match on the seller's line shows. The first two reproduce the
# bug: the buyer's date sat on the stamp's line above or below the label's, so "buyer signs" found nothing on its
# line, its value-below fallback read the seller's line, and "seller signs" won with the seller's date.
_FIRST_PAGE = ("RETAIL INSTALLMENT SALE CONTRACT\n"
               "Number of Payments  Amount of Payments  When Payments Are Due\n"
               "48                  $ 449.51            Monthly beginning 08/25/2031\n")
SIGNATURE_PAGES = {
    "date on the stamp line above": (
        "You agree to the terms of this contract.\n"
        "                 DocuSigned by:                          07/11/2031\n"
        "i Buyer Signs X  Pat Example                      Date\n"
        "                 4A3B2C1D0E9F47A8...\n"
        "Seller Signs EXAMPLE MOTORS  Date 07/12/2031  By X [signed]\n"),
    "date on the stamp line below": (
        "You agree to the terms of this contract.\n"
        "i Buyer Signs X  DocuSigned by:                   Date\n"
        "                 Pat Example                       07/11/2031\n"
        "                 4A3B2C1D0E9F47A8...\n"
        "Seller Signs EXAMPLE MOTORS  Date 07/12/2031  By X [signed]\n"),
    "both stamped, dates above": (
        "                 DocuSigned by:                    07/11/2031\n"
        "i Buyer Signs X  Pat Example                      Date\n"
        "                 4A3B2C1D0E9F47A8...\n"
        "                 DocuSigned by:                    07/12/2031\n"
        "Seller Signs X   Example Motors                   Date\n"
        "                 9F8E7D6C5B4A4321...\n"),
    "both stamped, date two lines below": (
        "Buyer Signs X    DocuSigned by:\n"
        "                 Pat Example\n"
        "                 4A3B2C1D0E9F47A8...                 Date 07/11/2031\n"
        "Seller Signs X   DocuSigned by:\n"
        "                 Example Motors\n"
        "                 9F8E7D6C5B4A4321...                 Date 07/12/2031\n"),
    "stamp inline": (
        "Buyer Signs X  DocuSigned by: Pat Example 4A3B2C1D0E9F47A8B6C5D4E3F2A1B0C9  Date 07/11/2031\n"
        "Seller Signs X  DocuSigned by: Example Motors 9F8E7D6C5B4A43210F1E2D3C4B5A6978  Date 07/12/2031\n"),
    "stamp with a timestamp": (
        "Buyer Signs X  DocuSigned by: Pat Example  7/11/2031 | 10:42:13 AM PDT   Date 07/11/2031\n"
        "Seller Signs X  DocuSigned by: Example Motors  7/12/2031 | 9:01:55 AM PDT   Date 07/12/2031\n"),
    "side by side on one line": (
        "     DocuSigned by:                                     DocuSigned by:\n"
        "i Buyer Signs X Pat Example  Date 07/11/2031  Co-Buyer Signs N/A Date N/A   "
        "Seller Signs X Example Motors Date 07/12/2031\n"
        "     4A3B2C1D0E9F47A8...                                  9F8E7D6C5B4A4321...\n"),
    "envelope id header": (
        "Docusign Envelope ID: 4A3B2C1D-0E9F-47A8-B6C5-D4E3F2A1B0C9\n"
        "i Buyer Signs X Pat Example   Date 07/11/2031   Co-Buyer Signs N/A  Date N/A\n"
        "Seller Signs X Example Motors  Date 07/12/2031  By X\n"),
}


@pytest.mark.parametrize("layout", sorted(SIGNATURE_PAGES))
def test_contract_date_is_the_buyers_signature_date_under_an_e_signature_stamp(vehicle, layout):
    recs, _, _ = fx.extract(fx.load_profile("vehicle_purchase"), [(1, _FIRST_PAGE), (5, SIGNATURE_PAGES[layout])], "x")
    got = {r["field"]: (r["value"], r["label"], r["page"]) for r in recs}
    assert got["contract_date"] == ("2031-07-11", "buyer signs", 5)
    assert "4A3B2C1D" not in json.dumps(recs)


def test_a_line_label_never_takes_a_date_from_another_signature_line(vehicle):
    # The buyer's line has no date anywhere near it; the seller's line, just below, must not give it one.
    prof = {"name": "t", "default_group": "g", "groups": {"g": {"global": True}},
            "fields": [{"group": "g", "name": "buyer_date", "type": "date", "labels": [["buyer signs", "line"]]},
                       {"group": "g", "name": "seller_date", "type": "date", "labels": [["seller signs", "line"]]}]}
    fx.check_profile(prof)
    page = ("i Buyer Signs X  DocuSigned by: Pat Example         Date\n"
            "Seller Signs EXAMPLE MOTORS  Date 07/12/2031  By X [signed]\n")
    recs, _, _ = fx.extract(prof, [(1, page)], "x")
    assert {r["field"]: r["value"] for r in recs} == {"seller_date": "2031-07-12"}


# The Truth in Lending boxes as OCR gives them from a scan: each header wrapped over two or three lines, its
# sentence below, the figure at the bottom of the box after a gap, and every line's columns a few characters off
# the others. The value-below rule's fixed window finds none of the four figures; "box_row" does.
TIL_BOXES = """Contract Date 07/11/2031
NEW   2031 FORD RANGER   XLT       Business

  ANNUAL            FINANCE           Amount            Total of            Total Sale
 PERCENTAGE        CHARGE            Financed          Payments           Price
   RATE           The dollar        The amount of      The amount you     The total cost of
 The cost of       amount the        credit provided   will have paid     your purchase on
your credit as    credit will        to you or on       after you have    credit, including
 a yearly rate.    cost you.        your behalf.       made all payments  your down payment of
                                                        as scheduled.     $2,500.00

     7.25 %          $6,512.88         $30,100.00          $36,612.88         $39,112.88

Your Payment Schedule Will Be:
"""
TIL_FIELDS = ("apr", "finance_charge", "amount_financed", "total_of_payments", "total_sale_price")


def _til(prof, text):
    recs, _, _ = fx.extract(prof, [(1, text)], "x.pdf", keep_span=True)
    return {r["field"]: r for r in recs if r["field"] in TIL_FIELDS}


def test_box_row_finds_each_figure_under_its_own_wrapped_header(vehicle):
    got = _til(fx.load_profile("vehicle_purchase"), TIL_BOXES)
    assert {k: (r["value"], r["label"]) for k, r in got.items()} == {
        "apr": ("7.25 %", "annual percentage rate"),
        "finance_charge": ("$6,512.88", "finance charge"),
        "amount_financed": ("$30,100.00", "amount financed"),
        "total_of_payments": ("$36,612.88", "total of payments"),
        "total_sale_price": ("$39,112.88", "total sale price"),
    }
    for r in got.values():  # each value carries its place on the page, for the review page's crop
        assert TIL_BOXES[r["span"][0]:r["span"][1]] == r["value"]


def test_box_row_is_opt_in(vehicle):
    prof = fx.load_profile("vehicle_purchase")
    for f in prof["fields"]:
        f.pop("box_row", None)
    got = _til(prof, TIL_BOXES)  # as before the key: only the amount financed, from its sentence
    assert {k: r["value"] for k, r in got.items()} == {"amount_financed": "$30,100.00"}


def test_box_row_on_a_group():
    prof = {"name": "t", "default_group": "til", "groups": {"til": {"global": True, "box_row": True}},
            "fields": [{"group": "til", "name": n, "type": t, "labels": [lab]} for n, t, lab in (
                ("apr", "percent", "annual percentage rate"), ("finance_charge", "money", "finance charge"),
                ("amount_financed", "money", "amount financed"), ("total_of_payments", "money", "total of payments"),
                ("total_sale_price", "money", "total sale price"))]}
    fx.check_profile(prof)
    got = _til(prof, TIL_BOXES)
    assert [got[k]["value"] for k in TIL_FIELDS] == ["7.25 %", "$6,512.88", "$30,100.00", "$36,612.88", "$39,112.88"]


def _figures(*cells):
    """The figures line with each (column, text) placed at its column."""
    line = ""
    for col, text in cells:
        line = line.ljust(col) + text
    return line


def test_box_row_never_gives_a_figure_to_a_neighbouring_header(vehicle):
    prof = fx.load_profile("vehicle_purchase")
    # TIL_BOXES' top header line sets the columns: cells at 2, 20, 38, 56 and 76, so edges at 14, 32.5, 50 and 70.
    head, tail = TIL_BOXES.split("     7.25 %")
    tail = tail.split("\n", 1)[1]
    # The finance charge box is blank: its neighbours' figures stay theirs, and it takes none.
    blank = head + _figures((5, "7.25 %"), (36, "$30,100.00"), (56, "$36,612.88"), (75, "$39,112.88")) + "\n" + tail
    got = _til(prof, blank)
    assert {k: r["value"] for k, r in got.items()} == {
        "apr": "7.25 %", "amount_financed": "$30,100.00", "total_of_payments": "$36,612.88",
        "total_sale_price": "$39,112.88"}
    # A figure astride the edge between two columns belongs to neither.
    edge = head + _figures((5, "7.25 %"), (28, "$6,512.88"), (40, "$30,100.00"), (56, "$36,612.88"),
                           (75, "$39,112.88")) + "\n" + tail
    got = _til(prof, edge)
    assert "finance_charge" not in got and got["apr"]["value"] == "7.25 %"
    assert got["amount_financed"]["value"] == "$30,100.00"
    # Two figures under one header: neither is taken.
    two = head + _figures((5, "7.25 %"), (15, "$6,512.88"), (26, "$612.88"), (40, "$30,100.00"), (56, "$36,612.88"),
                          (75, "$39,112.88")) + "\n" + tail
    got = _til(prof, two)
    assert "finance_charge" not in got and got["total_of_payments"]["value"] == "$36,612.88"


# ---------------------------------------------------------------- what the new rules may reach: sensitive values
def _one(ftype, labels, **opts):
    """A one-field profile: field "v" of type ftype in a global group."""
    prof = {"name": "t", "default_group": "g", "groups": {"g": {"global": True}},
            "fields": [{"group": "g", "name": "v", "type": ftype, "labels": labels, **opts}]}
    fx.check_profile(prof)
    return prof


def _run(prof, text):
    """{field: value} and the dropped counts. Tests assert on these, never print a value."""
    recs, dropped, _ = fx.extract(prof, [(1, text)], "x")
    return {r["field"]: r["value"] for r in recs}, dropped


BUYER = [["buyer signs", "line"]]


def test_a_line_label_near_never_takes_a_sensitive_neighbour():
    date = _one("date", BUYER)
    assert _run(date, "Buyer Signs X  Pat Example\nDOB 07/11/1990\n") == ({}, {"dob": 1})
    assert _run(date, "DOB 07/11/1990\nBuyer Signs X  Pat Example\n") == ({}, {"dob": 1})
    assert _run(date, "Buyer Signs X  Pat Example\nAcct 12-31-2030\n") == ({}, {"account": 1})
    # the label's own line has the date: the DOB below is never looked at
    assert _run(date, "Buyer Signs X  Pat Example  Date 07/11/2031\nDOB 07/11/1990\n") == ({"v": "2031-07-11"}, {})
    ref = _one("ref", BUYER)
    for value, kind in ((CARD, "card"), (ROUTING, "routing"), ("VIN 1M8GDM9AXKP042788", "vin")):
        assert _run(ref, f"Buyer Signs X\n{value}\n") == ({}, {kind: 1}), kind
    assert _run(_one("count", BUYER), f"Buyer Signs X\nSSN {SSN}\n") == ({}, {"ssn": 1})


def test_a_sensitive_nearest_neighbour_blanks_the_field():
    # Fails closed: the nearest value is sensitive, so the field stays empty rather than reaching past it to a
    # clean date further away, or to one the same distance below (above wins a tie).
    date = _one("date", BUYER)
    assert _run(date, "Buyer Signs X\nDOB 07/11/1990\n          07/11/2031\n") == ({}, {"dob": 1})
    assert _run(date, "DOB 07/11/1990\nBuyer Signs X\n          07/11/2031\n") == ({}, {"dob": 1})


def test_one_sensitive_value_reached_twice_counts_once():
    # "sale date" (value below) and "buyer signs" (nearest line) both reach the same DOB
    prof = _one("date", ["sale date", ["buyer signs", "line"]])
    assert _run(prof, "Sale Date      Buyer Signs X\nDOB 07/11/1990\n") == ({}, {"dob": 1})


def _boxes(first, second):
    prof = {"name": "t", "default_group": "g", "groups": {"g": {"global": True, "box_row": True}},
            "fields": [{"group": "g", "name": "first", "type": "ref", "labels": ["first box"]},
                       {"group": "g", "name": "second", "type": "ref", "labels": ["second box"]}]}
    fx.check_profile(prof)
    got, dropped = _run(prof, "First Box            Second Box\n" + first.ljust(21) + second + "\n")
    return sorted(got), dropped


def test_box_row_never_takes_a_sensitive_figure():
    assert _boxes(CARD, "R-55123") == (["second"], {"card": 1})
    assert _boxes(ROUTING, "123456789") == (["second"], {"routing": 1})  # 123456789 fails the ABA check


def test_a_dotted_card_number_is_never_a_money_value():
    got, dropped = _run(_one("money", [["total due", "line"]]), "Total Due  4111.1111.1111.1111\n")
    assert got == {} and dropped == {"card": 1}


# ---------------------------------------------------------------- edge cases of the nearest-line and box rules
def test_near_skips_not_and_renewal_words_and_looks_two_lines_only():
    assert _run(_one("date", BUYER, **{"not": ["beginning"]}),
                "Buyer Signs X\nMonthly beginning 08/25/2031\n") == ({}, {})
    renewal = "Buyer Signs X\nRenewal 08/25/2031\n"
    assert _run(_one("date", BUYER, document_date=True), renewal) == ({}, {})
    assert _run(_one("date", BUYER), renewal) == ({"v": "2031-08-25"}, {})  # the skip is document_date's
    date = _one("date", BUYER)
    assert _run(date, "Buyer Signs X\nPat Example\n4A3B2C1D\n07/11/2031\n") == ({}, {})  # three lines away
    assert _run(date, "Buyer Signs X\nPat Example\n07/11/2031\n") == ({"v": "2031-07-11"}, {})  # two


def test_near_never_takes_free_text():
    assert _run(_one("short_text", BUYER), "Buyer Signs X\nPat Example\n") == ({}, {})


def test_near_prefers_the_nearer_line_and_above_on_a_tie():
    date = _one("date", BUYER)
    assert _run(date, "07/10/2031\nBuyer Signs X\n07/12/2031\n") == ({"v": "2031-07-10"}, {})
    assert _run(date, "07/10/2031\nPat Example\nBuyer Signs X\n07/12/2031\n") == ({"v": "2031-07-12"}, {})


def test_a_line_label_is_refused_in_a_per_plan_group():
    prof = {"name": "t", "default_group": "g", "groups": {"g": {"per_plan": True}},
            "fields": [{"group": "g", "name": "v", "type": "date", "labels": BUYER}]}
    with pytest.raises(fx.ProfileError, match="per_plan"):
        fx.check_profile(prof)


def _money_boxes(n=2, box_row=True):
    names = ("first", "second", "third")[:n]
    return {"name": "t", "default_group": "g", "groups": {"g": {"global": True, "box_row": box_row}},
            "fields": [{"group": "g", "name": k, "type": "money", "labels": [f"{k} box"]} for k in names]}


def test_box_row_header_block_stops_at_eight_lines():
    filler = "some words           more words\n"
    page = "First Box            Second Box\n{}\n$12.00               $34.00\n"
    assert _run(_money_boxes(), page.format(filler * 7)) == ({"first": "$12.00", "second": "$34.00"}, {})
    assert _run(_money_boxes(), page.format(filler * 8)) == ({}, {})  # the header is the ninth line up


def test_box_row_figures_with_no_header_above():
    assert _run(_money_boxes(), "$12.00               $34.00\n\nFirst Box            Second Box\n") == ({}, {})


def test_box_row_empty_box_is_not_filled_from_below():
    page = ("First Box      Second Box      Third Box\n"
            "the cost       the amount      the total\n"
            "$12.00                         $56.00\n")
    # without the key, the value-below window under "Second Box" reaches the third box's figure
    assert _run(_money_boxes(3, box_row=False), page)[0].get("second") == "$56.00"
    assert _run(_money_boxes(3), page) == ({"first": "$12.00", "third": "$56.00"}, {})


# ---------------------------------------------------------------- auto_loan_statement
def test_lender_statement_fields():
    got, recs, _ = ext("auto_loan_statement", LENDER_STATEMENT)
    assert got == {
        "statement_date": "2031-03-05",
        "lender": "Example Motor Credit",
        "past_due_amount": "$0.00",
        "payment_amount": "$412.33",
        "payment_due_date": "2031-03-28",
        "maturity_date": "2036-07-28",
        "outstanding_balance": "$21,544.10",
    }
    out = json.dumps(recs)
    for bad in [VIN2, ACCOUNT, ADDRESS] + VNAMES:
        assert bad not in out, bad
    # a loan's maturity date is rightly years ahead: not flagged
    typed = [{**r, "type": fx.allowed_fields(fx.load_profile("auto_loan_statement"))[(r["group"], r["field"])]}
             for r in recs]
    assert fx.document_flags(typed, today=fx.dt.date(2031, 3, 10)) == {}


def test_generic_lender_statement_breakdown():
    got, recs, _ = ext("auto_loan_statement", GENERIC_STATEMENT)
    assert got == {
        "period_start": "2031-01-01",
        "period_end": "2031-01-31",
        "payment_amount": "$500.00",
        "principal_paid": "$410.00",
        "principal_paid_ytd": "$410.00",
        "interest_paid": "$90.00",
        "interest_paid_ytd": "$90.00",
        "principal_balance": "$18,250.00",
        "payoff_amount": "$18,301.12",
    }
    out = json.dumps(recs)
    assert VIN not in out and CONTRACT_NO not in out


def test_configure_profiles_dir_reaches_fields(tmp_path):
    """fields reads the profiles folder from the config each time, so a later configure() takes effect."""
    from doc_intake import configure
    (tmp_path / "only_one.json").write_text(json.dumps({"fields": [{"name": "total", "type": "money",
                                                                   "labels": ["total"]}]}))
    configure(profiles_dir=str(tmp_path))  # fresh_config (conftest) puts the host's back afterwards
    assert fx.profile_names() == ["only_one"]


# ---------------------------------------------------------------- usage: a meter reading with its unit
@pytest.mark.parametrize("text, want", [
    ("1,234 kWh", "1,234 kWh"),
    ("56.7 therms", "56.7 therms"),
    ("1 therm", "1 therm"),
    ("12 CCF", "12 CCF"),
    ("3 hcf", "3 HCF"),
    ("1.5 Mcf", "1.5 MCF"),
    ("4,500 gallons", "4,500 gallons"),
    ("45 gal.", "45 gal"),
    ("12 kgal", "12 kgal"),
    ("300 cubic feet", "300 cubic feet"),
    ("7 cu ft", "7 cu ft"),
    ("18 m3", "18 m3"),
    ("2.25 Dth", "2.25 Dth"),
    ("1234kwh", "1234 kWh"),            # the figure as printed, one space, the unit's usual spelling
    ("0.5 KWH", "0.5 kWh"),
])
def test_usage_keeps_the_figure_with_its_unit(text, want):
    assert _run(_one("usage", ["usage"]), f"Usage:   {text}\n") == ({"v": want}, {})


@pytest.mark.parametrize("text", [
    "742",                       # a bare figure: a reading needs its unit
    "$742 kWh",                  # an amount, not a reading
    "12345678901 kWh",           # an account number beside a unit word
    "1234-5678 kWh",             # the tail of a dashed number
    "1234 5678 kWh",             # or of a spaced one
    "1234567 kWh",               # more than six digits without thousands commas
    "0045123 gallons",           # a leading zero: a meter or account number
    "1,234,567,890 kWh",         # more than nine digits with them
    "7 kWhs",                    # a unit inside a longer word
    "12.5% kWh",
])
def test_usage_never_takes_a_number_beside_a_unit(text):
    got, _ = _run(_one("usage", ["usage"]), f"Usage:   {text}\n")
    assert got == {}


def test_usage_cut_from_a_longer_number_by_a_column_is_never_taken():
    # The value-below rule reads only the label's column, a slice of the line that starts inside the meter number
    # ("...5123 kWh"); the reading is checked again on the page, where the digits before it show it is no reading.
    head = "Service         Usage\n"
    text = head + "Meter No 0045123 kWh\n"
    assert text[len(head) + head.index("Usage") - 4:].startswith("5123 kWh")  # where the column's slice starts
    assert _run(_one("usage", ["usage"]), text) == ({}, {})
    # the same layout with a real reading below the label is read in full, not as the slice's part of it
    assert _run(_one("usage", ["usage"]), head + "Electric     1,234 kWh\n") == ({"v": "1,234 kWh"}, {})


def test_usage_values_go_through_the_detectors(monkeypatch):
    """Like every type: a reading that overlaps a detector's region is dropped and counted, never kept."""
    text = "Usage:   1,234 kWh\n"
    s = text.index("1,234")
    monkeypatch.setattr(detect, "find_spans", lambda t: [(s, s + 9, "account", "")] if t == text else [])
    assert _run(_one("usage", ["usage"]), text) == ({}, {"account": 1})


def test_usage_offered_without_page_text_keeps_the_type_check():
    """An Extraction made without its pages (a host calling offer itself) can't re-check a reading's edge on the
    page: it keeps the value, as every type does once it matched where it was found, and the detectors still run."""
    prof = _one("usage", ["usage"])
    ex = fx.Extraction(prof)
    ex.offer("g", None, None, "v", "usage", "742 kwh", 1, "usage", "x", (8, 15))
    assert [r["value"] for r in ex.records()] == ["742 kWh"]
    ex = fx.Extraction(prof)
    ex.offer("g", None, None, "v", "usage", "742 kWh", 1, "usage", "x", (8, 15), [(8, 15, "account", "")])
    assert ex.records() == [] and ex.dropped == {"account": 1}
    # with its pages, the page decides: here the reading is the tail of a longer number, so it is never kept
    ex = fx.Extraction(prof, pages=[(1, "Usage:  9742 kWh")])
    ex.offer("g", None, None, "v", "usage", "742 kWh", 1, "usage", "x", (9, 16))
    assert ex.records() == []


def test_usage_normalize_leaves_an_unmatched_value_alone():
    assert fx.normalize("usage", "  lots   of kWh ") == "lots of kWh"  # no figure: only the whitespace is tidied
    assert fx.normalize("usage", "") == ""


# ---------------------------------------------------------------- figures cut out of a longer number by a column slice
def test_a_money_figure_cut_from_a_longer_number_by_a_column_is_refused():
    # The value-below rule reads only the label's column: here that slice starts inside a meter number, at
    # "5123.50". Read on the page, digits run on before it, so it is no amount; with nothing else, the field is missing.
    head = "Service         Amount\n"
    text = head + "Meter No 0045123.50\n"
    assert text[len(head) + head.index("Amount") - 4:].startswith("5123.50")  # where the slice starts
    assert _run(_one("money", ["amount"]), text) == ({}, {})
    # the next candidate wins instead: another "Amount" further down with a whole figure below it
    text2 = text + "\nService         Amount\nElectric        $84.20\n"
    assert _run(_one("money", ["amount"]), text2) == ({"v": "$84.20"}, {})


@pytest.mark.parametrize("text, want", [
    ("Amount\n$1,234.56\n", "$1,234.56"),              # an ordinary figure below its label
    ("Amount\n1,234.56\n", "1,234.56"),
    ("Item      Qty    Amount\nWidget    2      12.00\n", "12.00"),   # a column of amounts
    ("Credit:   -5.00\n", "5.00"),                     # a lone minus is no digit run
    ("Total:   $5.00-$6.00\n", "$5.00"),                # a range of amounts, each whole
])
def test_ordinary_amounts_still_extract(text, want):
    label = "credit" if text.startswith("Credit") else ("total" if text.startswith("Total") else "amount")
    got, dropped = _run(_one("money", [label]), text)
    assert got["v"].lstrip("-") == want and dropped == {}


@pytest.mark.parametrize("text, s, e, ftype, cut", [
    ("12345678.00", 3, 11, "money", True),       # head: digits run on before
    ("1,234,567.89", 6, 12, "money", True),      # head: a digit group then a comma
    ("1234-5678.00", 5, 12, "money", True),      # head: a dashed number
    ("4,500.00", 0, 4, "money", True),           # tail: a slice ending inside "4,500.00"
    ("12.00/34", 0, 5, "money", True),           # tail: a slash then digits
    ("$1,234.56", 0, 9, "money", False),         # the whole figure
    ("-5.00", 1, 5, "money", False),             # a lone minus
    ("$5.00-$6.00", 0, 5, "money", False),       # a dash then "$": another figure, not more digits
    ("01/01/2030-01/14/2030", 0, 10, "date", False),   # a dash between dates is a range
    ("01/01/2030-01/14/2030", 11, 21, "date", False),
    ("12/31/2030", 6, 10, "year", True),         # a year taken out of a date
    ("Tax year 2030", 9, 13, "year", False),
    ("3.875 %", 0, 7, "percent", False),
])
def test_cut_from_longer(text, s, e, ftype, cut):
    assert fx.cut_from_longer(text, s, e, ftype) is cut


def test_box_row_and_table_amounts_still_extract():
    """Figures in a row of boxes and in a line-item table are whole on the page, so the guard leaves them alone."""
    prof = {"name": "t", "default_group": "g", "groups": {"g": {"global": True, "box_row": True}},
            "fields": [{"group": "g", "name": "first", "type": "money", "labels": ["first box"]},
                       {"group": "g", "name": "second", "type": "money", "labels": ["second box"]}]}
    fx.check_profile(prof)
    got, dropped = _run(prof, "First Box            Second Box\n" + "$1,234.56".ljust(21) + "$30,100.00\n")
    assert got == {"first": "$1,234.56", "second": "$30,100.00"} and dropped == {}
    table = {"name": "t", "default_group": "g", "groups": {"g": {"global": True}, "items": {}},
             "fields": [{"group": "g", "name": "total", "type": "money", "labels": ["total"]}],
             "tables": [{"group": "items", "header_min": 2, "row_requires": ["code"],
                         "columns": [{"name": "code", "type": "ref", "labels": ["code"]},
                                     {"name": "fee", "type": "money", "labels": ["fee"]}]}]}
    fx.check_profile(table)
    recs, dropped, _ = fx.extract(table, [(1, "Code      Fee\nD2740     $1,200.00\nD0120     60.00\n")], "x")
    assert sorted(r["value"] for r in recs if r["field"] == "fee") == ["$1,200.00", "60.00"] and dropped == {}
