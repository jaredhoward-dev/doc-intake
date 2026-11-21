"""Pair agreement: an approved invoice and an approved receipt for one charge must agree to the cent.

Two documents are a pair when one is an invoice task and the other a receipt task and either a number they both
carry matches (invoice_number or receipt_number, when the profiles extracted one) or they name the same vendor
(vendor_key: the host's config.vendor_key, else lower-case letters and digits) with dates at most PAIR_DAYS apart.
Each document pairs at most once: number matches first, then same-vendor candidates whose totals agree, then the
nearest dates. A host lists them, and its filing step can refuse either half of a mismatched pair. Output never
carries values other than vendor, date, amount and file name. Stdlib only.
"""
import datetime as dt
import os
import re
from decimal import Decimal, InvalidOperation

from .config import CFG

PAIR_DAYS = 3
KINDS = ("invoice", "receipt")
NUMBER_FIELDS = ("invoice_number", "receipt_number")


def _money(s):
    try:
        a = Decimal(re.sub(r"[$,\s]", "", s or ""))
    except InvalidOperation:
        return None
    return a.quantize(Decimal("0.01")) if a.is_finite() else None


def _date(s):
    try:
        return dt.date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def vendor_key(name):
    """The key two vendor names are compared by: the host's (config.vendor_key), else lower-case letters and
    digits run together."""
    if CFG.vendor_key:
        return CFG.vendor_key(name)
    return "".join(re.findall(r"[a-z0-9]+", (name or "").lower()))


def _num(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def documents(tasks, include=()):
    """One entry per source document of the approved, current invoice and receipt task files. tasks: [(task file
    name, task dict)]; include: task file names to take even when not approved (the preview's own task). The newest
    task file wins when two hold the same document (sha256)."""
    best = {}
    for name, t in tasks:
        if t.get("task") not in KINDS or t.get("superseded_by"):
            continue
        if t.get("approved") is not True and name not in include:
            continue
        for src in t.get("sources", []):
            v = {}
            for x in t.get("values", []):
                if x.get("file") == src.get("file"):
                    v.setdefault(x["field"], x["value"])
            d = {"task": name, "kind": t["task"], "file": os.path.basename(src.get("file") or ""),
                 "sha256": src.get("sha256"), "vendor": v.get("vendor") or "",
                 "vkey": vendor_key(v.get("vendor")), "date": _date(v.get("date") or v.get("invoice_date")),
                 "total": _money(v.get("total")), "created": t.get("created") or "",
                 "numbers": {_num(v[f]) for f in NUMBER_FIELDS if _num(v.get(f))}}
            old = best.get(d["sha256"])
            if old is None or d["created"] >= old["created"]:
                best[d["sha256"]] = d
    return list(best.values())


def match(docs):
    """(pairs, unmatched): pairs are dicts {invoice, receipt, agree, how}; unmatched the documents left over."""
    invs = [d for d in docs if d["kind"] == "invoice"]
    recs = [d for d in docs if d["kind"] == "receipt"]
    cands = []
    for i in invs:
        for r in recs:
            xref = bool(i["numbers"] & r["numbers"])
            days = abs((i["date"] - r["date"]).days) if i["date"] and r["date"] else None
            near = bool(i["vkey"]) and i["vkey"] == r["vkey"] and days is not None and days <= PAIR_DAYS
            if not (xref or near):
                continue
            agree = i["total"] is not None and i["total"] == r["total"]
            cands.append(((0 if xref else 1, 0 if agree else 1, days if days is not None else 99, i["file"],
                           r["file"]), i, r, agree, "number" if xref else "vendor and date"))
    cands.sort(key=lambda c: c[0])
    used, pairs = set(), []
    for _k, i, r, agree, how in cands:
        if i["sha256"] in used or r["sha256"] in used:
            continue
        used |= {i["sha256"], r["sha256"]}
        pairs.append({"invoice": i, "receipt": r, "agree": agree, "how": how})
    return pairs, [d for d in docs if d["sha256"] not in used]


def pair_for(sha, docs):
    """(pair, the other document) for the document with this sha256, or (None, None)."""
    pairs, _ = match(docs)
    for p in pairs:
        for me, other in (("invoice", "receipt"), ("receipt", "invoice")):
            if p[me]["sha256"] == sha:
                return p, p[other]
    return None, None


def _d(x):
    return x.isoformat() if x else "?"


def _t(x):
    return str(x) if x is not None else "?"


def describe(p):
    """One line for a pair: vendor, dates, totals and file names only."""
    i, r = p["invoice"], p["receipt"]
    state = "agree" if p["agree"] else "MISMATCH"
    sign = "=" if p["agree"] else "!="
    return (f"pair {state}: {i['vendor'] or r['vendor']}; invoice {_d(i['date'])} {_t(i['total'])} {sign} receipt "
            f"{_d(r['date'])} {_t(r['total'])}; {i['file']} + {r['file']} (by {p['how']})")


def describe_doc(d):
    return f"unmatched {d['kind']}: {d['vendor'] or '?'} {_d(d['date'])} {_t(d['total'])}; {d['file']}"
