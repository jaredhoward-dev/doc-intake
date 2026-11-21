"""The settable "today" (config.today): document dates are judged against it, never against the real calendar.

The pinned dates are years from the real date, so a check still reading the real clock would fail here.
"""
import datetime as dt

import doc_intake
from doc_intake import fields as fx
from doc_intake.config import CFG
from helpers import drop, rec, run  # noqa: F401  (rec is a fixture)

PINNED = dt.date(2040, 1, 1)
NEAR = (PINNED + dt.timedelta(days=30)).isoformat()
FAR = (PINNED + dt.timedelta(days=90)).isoformat()


def test_default_is_the_real_calendar():
    assert CFG.today() == dt.date.today()


def test_pinned_today_judges_document_dates():
    doc_intake.configure(today=lambda: PINNED)
    assert fx.suspicious("invoice_date", "date", NEAR) is None
    assert fx.suspicious("invoice_date", "date", FAR) == "outside 2000 to 60 days from today"
    assert fx.plausible_document_date(NEAR) and not fx.plausible_document_date(FAR)
    recs = [{"field": "invoice_date", "type": "date", "value": NEAR}, {"field": "date", "type": "date", "value": FAR}]
    assert fx.document_flags(recs) == {1: "outside 2000 to 60 days from today"}


def test_explicit_today_argument_still_wins():
    doc_intake.configure(today=lambda: PINNED)
    assert fx.suspicious("invoice_date", "date", NEAR, today=PINNED - dt.timedelta(days=365)) is not None


def test_fresh_config_puts_the_real_calendar_back():
    import host
    doc_intake.configure(today=lambda: PINNED)
    host.configure()
    assert CFG.today() == dt.date.today() != PINNED


def test_labels_calls_dates_near_or_far_from_pinned_today(rec, capsys):
    doc_intake.configure(today=lambda: PINNED)
    m, d, y = NEAR[5:7], NEAR[8:10], NEAR[:4]
    drop(rec, "inv.txt", f"Example Co\nInvoice number INV-1\nInvoice date {m}/{d}/{y}\nAmount due $5.00\n")
    assert run(["fields", "--task", "invoice", "--labels", "inbox/inv.txt"]) == 0
    assert "in order: near; scan's document date: past or near" in capsys.readouterr().out
    doc_intake.configure(today=lambda: PINNED - dt.timedelta(days=60))
    assert run(["fields", "--task", "invoice", "--labels", "inbox/inv.txt"]) == 0
    assert "in order: far; scan's document date: far-future" in capsys.readouterr().out
