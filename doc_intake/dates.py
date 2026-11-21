"""Dates in a document's (redacted) text: the date a document is about, for its file name.

Payment, order and transaction dates come first (when the money moved), then statement, invoice and issue dates,
then the first date in the text. Renewal, expiry and next-bill dates and dates more than 60 days ahead are passed
over unless the text has nothing else. Stdlib only.
"""
import re

from . import fields as fx


def _first_date(text, plausible=True):
    """The first date that is not a renewal, expiry or next-bill date and not more than 60 days ahead; with
    plausible "near", the first not more than 60 days ahead; with False, the first date at all. Else None."""
    for m in re.finditer(fx.DATE, text, re.I):
        d = fx.parse_date(m.group(0))
        if not d:
            continue
        if plausible is True and not fx.plausible_document_date(d, text[max(0, m.start() - 40):m.start()]):
            continue
        if plausible == "near" and not fx.plausible_document_date(d):
            continue
        return d
    return None


# Payment, order and transaction dates first: they are when the money moved.
DATE_LABELS = (r"payment\s+date|date\s+paid|paid\s+on|transaction\s+date|order\s+date|purchase\s+date"
               r"|registration\s+date|date\s+of\s+purchase",
               r"statement\s+date|invoice\s+date|bill(?:ing)?\s+date|date\s+issued|issue\s+date|issued(?:\s+on)?"
               r"|date\s+of\s+issue|date\s+of\s+service|service\s+date|pay\s+date|check\s+date")
_LABELED_DATE = [re.compile(r"\b(?:" + labs + r")\b\D{0,20}?(" + fx.DATE + ")", re.I) for labs in DATE_LABELS]


def _labeled_date(text, plausible=True):
    """The first date right after a payment, order or transaction label, else after a label such as "statement
    date" or "issued". With plausible, never one more than 60 days ahead. None when there is none."""
    for rx in _LABELED_DATE:
        for m in rx.finditer(text):
            d = fx.parse_date(m.group(1))
            if d and (not plausible or fx.plausible_document_date(d)):
                return d
    return None


def document_date(text):
    """Labeled, then any date, preferring ones that are not renewal or expiry dates nor over 60 days ahead: a
    far-future date is taken only when the text has no other."""
    return (_labeled_date(text) or _first_date(text) or _first_date(text, "near") or _labeled_date(text, False)
            or _first_date(text, False))



def _date_after(text, labels):
    m = re.search(r"(?i)\b(?:" + labels + r")\b\D{0,20}(" + fx.DATE + ")", text)
    return fx.parse_date(m.group(1)) if m else None

