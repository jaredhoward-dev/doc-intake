"""Write the synthetic records folder that tests/test_golden.py runs the fake host over.

    python3 tests/fixtures/golden/make_fixture.py     # rewrites tests/fixtures/golden/records/

Every value is made up: example vendors, a made-up routing number that passes the checksum, test card numbers.
Text and email files only, so the run needs no PDF or OCR tool and its output is the same on any machine. No
SSN-shaped value is written (a pre-commit guard would block one); the detectors are exercised with the other kinds.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "records")

RECEIPT_SOFTWARE = """EXAMPLE SOFTWARE CO
Receipt
01/09/2030
Monthly plan     9.00
Tax              0.54
TOTAL            $9.54
Paid with card ending 1111
Thank you
"""
INVOICE_SOFTWARE = """EXAMPLE SOFTWARE CO
Invoice
Invoice number EX-1001
Invoice date 01/08/2030
Monthly plan     9.00
Tax              0.54
Amount due       $9.54
"""
RECEIPT_PRINT = """EXAMPLE PRINT SHOP
Receipt
Transaction date 02/03/2030
Flyers           20.00
TOTAL            $21.00
Thank you for your purchase
"""
INVOICE_PRINT = """EXAMPLE PRINT SHOP
Invoice
Invoice number PS-2002
Invoice date 02/02/2030
Flyers           20.00
Amount due       $20.00
"""
W2 = """Form W-2 Wage and Tax Statement 2030
Employer identification number 12-3456789
1 Wages, tips, other compensation     2 Federal income tax withheld
52,000.00                             6,100.00
3 Social security wages               4 Social security tax withheld
52,000.00                             3,224.00
5 Medicare wages and tips             6 Medicare tax withheld
52,000.00                             754.00
"""
PAYSTUB = """Example Payroll Co   Earnings Statement
Pay date: 01/15/2030    Pay period: 01/01/2030 - 01/14/2030
Employee ID 00123456
Earnings        Rate     Hours    Current      YTD
Regular         25.00    80.00    2,000.00     2,000.00
Gross Pay                         2,000.00     2,000.00
Federal Income Tax                  200.00       200.00
State Income Tax                     81.00        81.00
Social Security                     124.00       124.00
Medicare                             29.00        29.00
401(k)                              100.00       100.00
Net Pay                           1,372.00     1,372.00
Direct deposit  Routing 111111118  Account 12345678901   1,372.00
"""
SECRET = "Example site login\nUsername: someone@example.com\nPassword: hunter22\n"
UTILITY = """EXAMPLE WATER UTILITY
Statement date 01/20/2030
Service address on file
Billing period January 2030
Water usage charge for the month of January, metered at the house, with the
base service fee and the sewer charge for the same billing period, as read on
the meter by the field crew on their regular route through the neighborhood.
Amount due $48.20 by 02/10/2030
Questions: call the number on the back of this statement
"""
UTILITY_RESCAN = UTILITY.replace("field crew", "field  crew").replace("Questions:", "Questions -")
NOTES = "Shopping list\nbread\nmilk\neggs\nlight bulbs for the hall\n"
EMAIL = """From: Example Garden Store <orders@example.com>
To: someone@example.com
Subject: Your order receipt
Date: Mon, 04 Mar 2030 10:15:00 -0500
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

EXAMPLE GARDEN STORE
Order receipt
Order date 03/04/2030
Seed starter kit   15.00
Tax                 0.90
Order total        $15.90
Paid with card ending 4444
"""

FILES = {
    # already in the records root: one exact copy and one near copy in the inbox below
    "Archive/2030/2030-01-20 - Water bill.txt": UTILITY,
    "Archive/2030/2030-01-09 - Software receipt (old copy).txt": NOTES.replace("bread", "flour"),
    "Taxes/2029/notes.txt": "Nothing to see here: a plain note with no document words in it.\n",
    # the inbox
    "inbox/receipt-software.txt": RECEIPT_SOFTWARE,
    "inbox/invoice-software.txt": INVOICE_SOFTWARE,
    "inbox/receipt-print.txt": RECEIPT_PRINT,
    "inbox/invoice-print.txt": INVOICE_PRINT,
    "inbox/w2-2030.txt": W2,
    "inbox/paystub.txt": PAYSTUB,
    "inbox/logins.txt": SECRET,
    "inbox/water-copy.txt": UTILITY,
    "inbox/water-rescan.txt": UTILITY_RESCAN,
    "inbox/list.txt": NOTES,
    "inbox/garden-order.eml": EMAIL,
}


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    for rel, text in FILES.items():
        p = os.path.join(OUT, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    print(f"wrote {len(FILES)} files under {os.path.relpath(OUT)}")


if __name__ == "__main__":
    main()
