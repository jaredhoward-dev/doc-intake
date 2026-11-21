"""Profile-driven field extraction: pull only an allowlisted set of fields out of a document's text.

A profile (<name>.json in PROFILE_DIR, set by config.configure) names its fields, the label synonyms that introduce
each one, and the value type each value must match. Nothing outside a profile's fields is ever returned, and every value is checked
against the sensitive detectors (detect.py) before it is kept: a value that overlaps a detected span, or that a
detector flags when read with its label, is dropped and counted by kind.

How values are found (heuristics, checked by the user in the preview before approval):
- A label on a line, and the typed value after it on that line ("Deductible   $1,500"). When several labels sit on
  one line as separate columns (two or more spaces apart, as on tax forms), each owns the text up to the next.
- Otherwise the value below the label, in the same columns, within the next three non-empty lines (tax-form boxes).
- Benefits tables: a line of plan names (no values) starts a section; a following row with one value per plan
  gives each plan its value. With one plan, only the first value cell counts (in-network, usually).
- Line-item tables (treatment estimates): a header row of column labels, then rows assigned by column position.

Per-field options for forms laid out in fixed columns (the Closing Disclosure):
- a label given as [text, "last"] takes the last value on the line (a total after a rate, or the final figure
  after the Loan Estimate's);
- "before": true takes the value just before the label on the line ("0.25 % of Loan Amount (Points)"); a label given
  as [text, "before"] does the same for that label only ("72  $500.00  Monthly beginning 08/01/2030");
- "column_before": "<header label>" or [label, n] ignores everything at or right of that header's column (its n-th
  match, from 0, on the first line of the page that has that many) so only the borrower's columns count (the CD's
  second "At Closing" is the Seller-Paid one); with no such header line on the page, nothing is cut; such fields,
  like "each" ones, never take a value from the lines below (a blank row would take the next row's);
- a label given as [text, "line"] owns the rest of its line, past other labels on it, and needs no cell start
  ("| Buyer Signs X Date 07/11/2030" in a scanned box: the signature line's date, where "Date" alone is no label);
  with no value on its line it takes the nearest one within two non-empty lines above or below, never on or past
  another "line" label's line (an e-signature stamp is taller than a line of text, so its date can sit just above or
  below; the seller's line below must not give the buyer's date), ahead of the next label but behind its own line's;
- "each": true keeps every matching line as its own row (several "Title -" charges), not just the first;
- "anywhere": "earliest" (dates) falls back to the earliest plausible date in the text, not the first one;
- "link_month": 0 or 1 (dates) takes the first or last day of the month a link's query names ("...?year=2030&month=9",
  as in "see your bill online" emails), below any labeled date or range;
- "name_date": true (dates), only when nothing else gave the field a value: the YYYY-MM-DD at the start of the file
  name (fetchers name files by the date they were sent); the preview says to check it;
- "below_lines": n looks for the value in up to n non-empty lines below the label instead of three (the Truth in
  Lending boxes of a retail installment contract put a sentence between the label and its figure); with
  "below_values_only": true, a line below counts only when its part in the label's column holds nothing but values
  (the box's sentence can hold a figure of its own: "including your down payment of $500.00").
"""
import datetime as dt
import json
import os
import re

from . import detect
from .config import CFG

# Set by config.configure: the folder of profile JSON files, and the known-senders map (optional).
PROFILE_DIR = None
VENDORS_FILE = None

# Cents, or up to four decimals for rates such as prepaid interest per day ("$54.5548 per day").
MONEY = r"(?:\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2,4})?|\$\s?\d+(?:\.\d{2,4})?|\d{1,3}(?:,\d{3})+\.\d{2,4}|\d+\.\d{2,4})"
PERCENT = r"\d{1,3}(?:\.\d+)?\s?%"
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE = (r"(?:\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})|\d{1,2}-\d{1,2}-\d{4}|\d{4}-\d{1,2}-\d{1,2}"
        r"|" + _MONTH + r"\s+\d{1,2}\s*,?\s+\d{4})")
# "October 1 - October 31, 2026" (billing periods on invoices): the start's year comes from the end
DATE_RANGE = re.compile(r"(?<![\w])(" + _MONTH + r"\s+\d{1,2})(?:\s*,?\s+(\d{4}))?\s*(?:-|–|—|to|through)\s*("
                        + _MONTH + r"\s+\d{1,2}\s*,?\s+\d{4})(?![\w])", re.I)
TYPES = {
    "money": MONEY,
    "percent": PERCENT,
    "cost": (r"(?:" + MONEY + r"|" + PERCENT + r")(?:\s*(?:copay|coinsurance))?"
             r"(?:\s*(?:after|before)\s+(?:the\s+)?deductible)?|no\s+charge|not\s+covered|covered\s+in\s+full"),
    "limit": MONEY + r"(?:\s*/\s*" + MONEY + r")*",
    "date": DATE,
    "duration": r"\d{1,2}\s*(?:months?|mos?\.?|days?|years?)|no\s+waiting\s+period|none",
    "eligibility": (r"adults?(?:\s+and\s+children)?(?:\s+covered)?|children\s+only|child(?:ren)?\s+only"
                    r"|(?:up\s+)?to\s+age\s+\d{2}|under\s+(?:age\s+)?\d{2}|not\s+covered|covered|yes|no"),
    "basis": (r"UCR|usual,?\s+(?:and\s+)?customary(?:\s+and\s+reasonable)?|reasonable\s+and\s+customary|R&C|MAC"
              r"|maximum\s+allowable(?:\s+charge)?|maximum\s+plan\s+allowance|MPA|in-network\s+fee\s+schedule"
              r"|\d{2}(?:th)?\s+percentile(?:\s+of\s+[A-Z]+)?"),
    "money_or_none": MONEY + r"|none|not\s+available|no\s+carryover",
    "plan_type": (r"HDHP|high[\s-]deductible(?:\s+health)?(?:\s+plan)?|HSA[\s-](?:eligible|qualified|compatible)"
                  r"|PPO|HMO|EPO|POS|DPPO|DHMO|HRA"),
    "w2_box12": r"(?:[A-HJ-NP-Z]|AA|BB|DD|EE|FF|GG|HH|II)\s+" + MONEY,
    "form_1099": r"1099-?(?:NEC|MISC|INT|DIV|K|G)",
    "currency": r"USD|EUR|GBP|CAD|AUD|JPY|CHF|MXN|INR",
    "pay_method": (r"visa|mastercard|master\s+card|amex|american\s+express|discover|debit|credit|card|cash|check"
                   r"|cheque|zelle|venmo|paypal|apple\s+pay|google\s+pay|ach|money\s+order"),
    "policy_type": (r"homeowners?|renters?|landlord|dwelling(?:\s+fire)?|condo(?:minium)?|auto(?:mobile)?|umbrella"
                    r"|term\s+life|whole\s+life|life|disability|flood|earthquake|pet"),
    "ref": r"(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{2,20}",
    "cdt_code": r"D\d{4}",
    "tooth_area": (r"#?(?:[1-9]|[12]\d|3[0-2])|[A-T]|UR|UL|LR|LL|upper(?:\s+(?:left|right))?|lower(?:\s+(?:left|right))?"
                   r"|quad(?:rant)?\s?[1-4]|full\s+mouth|(?:[1-9]|[12]\d|3[0-2])\s?-\s?(?:[1-9]|[12]\d|3[0-2])"),
    "year": r"(?:19|20)\d{2}",
    "count": r"(?<![.,\d])\d{1,3}(?![.,]?\d)",
    "weight_lbs": r"\d{1,2},?\d{3}(?:\s*(?:lbs?\.?|pounds))?",
    "short_text": r"[A-Za-z][A-Za-z ,.&/()'+-]{2,59}",
    "org_name": r"[A-Za-z][A-Za-z0-9 ,.&'()+-]{2,59}",
    "plan_name": r"[A-Za-z0-9][A-Za-z0-9 &+/().,-]{1,59}",
}
# Types matched case-insensitively. The rest (codes, refs) are case-sensitive.
_CI = {"cost", "date", "duration", "eligibility", "basis", "money_or_none", "plan_type", "pay_method", "policy_type",
       "tooth_area", "currency", "form_1099", "weight_lbs"}
_RX = {t: re.compile(r"(?<![\w$])(?:" + p + r")(?![\w%])", re.I if t in _CI else 0) for t, p in TYPES.items()}
_FULL = {t: re.compile(r"(?:" + p + r")", re.I if t in _CI else 0) for t, p in TYPES.items()}
# Types a host registered with title_case=True: normalized to title case, short or alphanumeric words as printed.
TITLE_CASE = set()
# The registered type a "model_from" field strips from the front of its source value (register_type model_prefix).
MODEL_PREFIX = None


def register_type(name, pattern, ci=False, title_case=False, model_prefix=False):
    """Add a value type that a host's own profiles use: its regex (matched case-insensitively with ci). title_case:
    normalize values to title case ("EXAMPLE MAKE" -> "Example Make"). model_prefix: a "model_from" field takes
    what follows a value of this type. Call it before loading a profile that names the type."""
    global MODEL_PREFIX
    TYPES[name] = pattern
    if ci:
        _CI.add(name)
    _RX[name] = re.compile(r"(?<![\w$])(?:" + pattern + r")(?![\w%])", re.I if ci else 0)
    _FULL[name] = re.compile(r"(?:" + pattern + r")", re.I if ci else 0)
    if title_case:
        TITLE_CASE.add(name)
    if model_prefix:
        MODEL_PREFIX = name
# Free-text types: never searched inside a line, only taken as a whole cell or line.
WHOLE = {"short_text", "org_name", "plan_name"}
CELL_SPLIT = re.compile(r"\s{2,}|\t|\s\|\s")
_STOP = re.compile(r"^(?:your|our|how|about|compare|comparing|choosing|when|if|the following|what|why|this|these)\b",
                   re.I)
_DIGITS3 = re.compile(r"\d{3,}")


class ProfileError(ValueError):
    pass


# ---------------------------------------------------------------- profiles
# A known-senders map may sit in the profiles folder; it is never a profile. The file senders_file names is skipped
# whatever it is called (_is_senders_map). NOT_PROFILES stays as a fallback for a host that keeps the map beside its
# profiles under the conventional name (the map's own top-level key, {"vendors": [...]}) without configuring
# senders_file: such a file was never listed before, and listing it now would offer a non-profile as a task.
NOT_PROFILES = {"vendors"}


def _is_senders_map(path):
    """True when path is the configured known-senders map (compared by real path, so a relative or linked
    senders_file still matches)."""
    if not VENDORS_FILE:
        return False
    try:
        return os.path.realpath(path) == os.path.realpath(VENDORS_FILE)
    except (OSError, ValueError):
        return False


def _not_a_profile(name):
    return name in NOT_PROFILES or _is_senders_map(profile_path(name))


def profile_names():
    if not PROFILE_DIR:
        return []
    return sorted(n[:-5] for n in os.listdir(PROFILE_DIR) if n.endswith(".json") and not _not_a_profile(n[:-5]))


def load_profile(name):
    if not re.fullmatch(r"[a-z0-9_]+", name or ""):
        raise ProfileError(f"no profile named {name!r}")
    if not PROFILE_DIR:
        raise ProfileError("no profile folder is configured")
    if _not_a_profile(name):
        raise ProfileError(f"{name!r} is the known-senders map, not a profile; profiles: {', '.join(profile_names())}")
    p = profile_path(name)
    try:
        with open(p, encoding="utf-8") as f:
            prof = json.load(f)
    except FileNotFoundError:
        raise ProfileError(f"no profile named {name!r}; profiles: {', '.join(profile_names())}") from None
    check_profile(prof)
    return prof


def profile_path(name):
    return os.path.join(PROFILE_DIR, name + ".json")


def check_profile(prof):
    groups = prof.get("groups") or {}
    seen = set()
    for f in prof.get("fields", []):
        if f["type"] not in TYPES:
            raise ProfileError(f"{prof['name']}: field {f['name']} has unknown type {f['type']}")
        if f["group"] not in groups:
            raise ProfileError(f"{prof['name']}: field {f['name']} has unknown group {f['group']}")
        key = (f["group"], f["name"])
        if key in seen:
            raise ProfileError(f"{prof['name']}: duplicate field {f['group']}.{f['name']}")
        seen.add(key)
    for t in prof.get("tables", []):
        for c in t["columns"]:
            if c["type"] not in TYPES:
                raise ProfileError(f"{prof['name']}: column {c['name']} has unknown type {c['type']}")


def allowed_fields(prof):
    """{(group, field): type} for every field and table column the profile allows."""
    out = {(f["group"], f["name"]): f["type"] for f in prof.get("fields", [])}
    for t in prof.get("tables", []):
        for c in t["columns"]:
            out[(t["group"], c["name"])] = c["type"]
    return out


def _label_rx(label):
    # An en dash in a label is kept and matches an en dash or a hyphen ("Title –" on the Closing Disclosure).
    toks = ["[–-]" if t == "–" else re.escape(t)
            for t in re.split(r"[^A-Za-z0-9+#%–]+|(–)", label) if t]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\W*".join(toks) + r"(?![A-Za-z0-9])", re.I)


def _compile(prof):
    fields = []
    for f in prof.get("fields", []):
        labels = []
        for rank, lab in enumerate(f.get("labels", [])):
            text, idx = (lab, 0) if isinstance(lab, str) else (lab[0], lab[1])
            labels.append((rank, text, idx, _label_rx(text)))
        nots = [_label_rx(n) for n in f.get("not", [])]
        fields.append({**f, "_labels": labels, "_not": nots})
    return fields


# ---------------------------------------------------------------- values
def normalize(ftype, v):
    v = " ".join(v.split()).strip(" .,:;")
    if ftype == "date":
        return parse_date(v) or v
    if ftype == "org_name":
        return trim_vendor(v)
    if ftype == "pay_method":
        low = v.lower()
        if low in ("cash",):
            return "cash"
        if low in ("check", "cheque"):
            return "check"
        if low in ("zelle", "ach"):
            return low
        if low == "money order":
            return "money-order"
        if low in ("venmo", "paypal"):
            return "other"
        return "card"
    if ftype in ("plan_type", "form_1099", "currency", "cdt_code"):
        v = v.upper()
        if ftype == "form_1099" and "-" not in v:
            v = "1099-" + v[4:]
        return v
    if ftype == "policy_type":
        return v.lower()
    if ftype == "weight_lbs":
        return re.sub(r"\D", "", v)
    if ftype in TITLE_CASE:
        # "EXAMPLE MAKE" -> "Example Make"; short or alphanumeric words (XLE, SR5, A4, F-150) as printed
        return " ".join(w.title() if (w.isalpha() and len(w) > 3 and (w.isupper() or w.islower()))
                        or w.islower() else w for w in v.split())
    return v


def parse_date(s):
    s = re.sub(r"\s+,", ",", " ".join(s.split()))
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
                "%b. %d, %Y", "%b. %d %Y"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if 1950 <= d.year <= 2100:
            return d.isoformat()
    return None


def valid_value(ftype, v):
    """True when v is entirely one value of the type (used for model output and whole-cell types)."""
    if not isinstance(v, str) or not v.strip() or len(v) > 80 or ftype not in _FULL:
        return False
    v = " ".join(v.split())
    if not _FULL[ftype].fullmatch(v):
        return False
    if ftype in ("org_name", "short_text", "plan_name") and _DIGITS3.search(v):
        return False  # street numbers, ZIP codes and IDs stay out of free-text fields
    if ftype in ("org_name", "short_text", "plan_name") and sum(c.isalpha() for c in v) < 3:
        return False
    return True


# Where a vendor or payer name runs into the next label ("Example Co, PBC Bill to"), cut it there.
_TRIM = re.compile(r"\n|\s{2,}|\t|\s\|\s|(?<![A-Za-z])(?:bill(?:ed)?\s+to|ship(?:ped)?\s+to|sold\s+to|remit\s+to"
                   r"|invoice\s*(?:number|no\b|#|date)|receipt\s*(?:number|no\b|#)|date\s+(?:of\s+issue|paid|due)"
                   r"|page\s+\d|account\s*(?:number|no\b|#|id))", re.I)


def trim_vendor(v):
    v = _TRIM.split(v or "", maxsplit=1)[0]
    return " ".join(v.split()).strip(" .,:;-|")


_YEAR = re.compile(r"(?:19|20)\d{2}")


def invoice_number_ok(v):
    """An invoice number holds a non-year token: a letter, a dash or 6+ digits, and is never just a year."""
    v = (v or "").strip()
    if not v or _YEAR.fullmatch(v) or re.fullmatch(r"\d{1,4}", v) or parse_date(v):
        return False
    return bool(re.search(r"[A-Za-z]|-|\d{6,}", v))


VALIDATORS = {"invoice_number": invoice_number_ok}


def load_vendors():
    """The known-senders map, compiled: (vendors, stripe markers, bill-to pattern, (event markers, heading)).
    With no map configured, nothing is known."""
    spec = {"vendors": []}
    if VENDORS_FILE:
        with open(VENDORS_FILE, encoding="utf-8") as f:
            spec = json.load(f)
    out = []
    for v in spec["vendors"]:
        pats = [re.compile(r"(?<![A-Za-z0-9.-])" + re.escape(d) + r"(?![A-Za-z0-9-])", re.I) for d in v.get("domains", [])]
        pats += [re.compile(p, re.I) for p in v.get("patterns", [])]
        out.append((v["name"], pats, v.get("header_lines"), v.get("kind"), v.get("capture")))
    stripe = spec.get("stripe", {})
    ev = spec.get("events", {})
    events = ([re.compile(p, re.I) for p in ev.get("markers", [])], re.compile(ev.get("heading", r"(?!)"), re.I))
    return out, [re.compile(p, re.I) for p in stripe.get("markers", [])], re.compile(stripe.get("bill_to", "bill to"),
                                                                                       re.I), events


def sender_entry(name):
    """The known-senders map's entry for a vendor name (case-insensitive), else {}: a host may keep its own keys
    there."""
    if not VENDORS_FILE or not name:
        return {}
    with open(VENDORS_FILE, encoding="utf-8") as f:
        for v in json.load(f)["vendors"]:
            if v["name"].casefold() == name.casefold():
                return v
    return {}


def known_vendor(pages, kind=None):
    """(clean name, page, how) for a sender in the known-senders map (VENDORS_FILE), matched by domain or header
    pattern; else a Stripe-hosted invoice's "from" business name (the cell before "Bill to"), trimmed; else None. kind limits the
    map to entries of that kind ("dealer"); a "capture" entry's name is its pattern's first group, trimmed."""
    vendors, stripe, bill_to, (ev_markers, ev_heading) = load_vendors()
    for name, pats, head, vkind, capture in vendors:  # map order: specific before general (a marketplace first)
        if kind and vkind != kind:
            continue
        for pno, text in (pages[:1] if head else pages):
            if head:
                text = "\n".join(text.split("\n")[:head])
            for p in pats:
                m = p.search(text)
                if not m:
                    continue
                if capture:
                    found = trim_vendor(" ".join(m.group(1).split()))
                    if valid_value("org_name", found) and not detect.find_spans(found):
                        return found, pno, name
                    continue
                return name, pno, "known sender"
    if kind:
        return None
    for pno, text in pages[:1]:
        if not any(m.search(text) for m in stripe):
            continue
        lines = text.split("\n")
        for i, ln in enumerate(lines[:30]):
            m = bill_to.search(ln)
            if not m:
                continue
            before = trim_vendor(ln[:m.start()])
            if not before:  # "Bill to" alone on its line: the business name is the line above
                before = next((trim_vendor(x) for x in reversed(lines[max(0, i - 3):i]) if x.strip()), "")
            if valid_value("org_name", before) and not re.search(r"invoice|receipt|page", before, re.I):
                return before, pno, "stripe: from name"
            break
    for pno, text in pages[:1]:  # race and event registrations: the event's heading ("2030 Example City Marathon")
        if not any(m.search(text) for m in ev_markers):
            continue
        strong = re.compile(r"\b(?:marathon|half\s+marathon|[0-9]+(?:\.[0-9]+)?\s?k|ultra|triathlon|duathlon|relay|"
                            r"fun\s+run|5\s?miler|[0-9]+\s?miler)\b", re.I)
        weak = None
        for ln in text.split("\n")[:40]:
            if ":" in ln.split("@")[0][:60] and re.search(r"\S\s*:", ln):
                # a "Label: value" line ("Age on Race Day: 24", "Event Cost: $115.00") is a field, not the heading
                continue
            for _, _, c in _cells(ln):
                raw = " ".join(c.split())
                if raw.endswith(":"):
                    continue
                name = trim_vendor(re.sub(r"^(?:19|20)\d{2}\s+", "", raw))
                if not (ev_heading.search(name) and valid_value("org_name", name)):
                    continue
                if strong.search(name):
                    return name, pno, "event heading"
                weak = weak or name
        if weak:
            return weak, pno, "event heading"
    return None


# ---------------------------------------------------------------- suspicious values (preview flags)
_VENDOR_JUNK = re.compile(r"(?<![A-Za-z])(?:bill(?:ed)?\s+to|ship\s+to|sold\s+to|shown|page|invoice|receipt|charges?"
                          r"|number|total|amount|this|date|due|period)(?![A-Za-z])", re.I)
_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}


_NOT_DOC_DATE = re.compile(r"(?i)(?:renew\w*|expir\w*|expires?|next\s+(?:bill\w*|payment|charge|renewal)|valid\s+"
                           r"(?:until|through|thru)|ends?\s+on|auto-?pay\s+on|due\s+(?:on|by)?)\W{0,20}$")


_RENEWAL = re.compile(r"(?i)\b(?:renew\w*|expir\w*|next\s+(?:bill\w*|payment|charge|renewal)|valid\s+(?:until|through|thru)"
                      r"|auto-?pay)\b")


def plausible_document_date(iso, before=""):
    """False for a renewal, expiry or next-bill date (by the words just before it) or one over 60 days after the
    configured today (config.CFG.today)."""
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return False
    if d > CFG.today() + dt.timedelta(days=60):
        return False
    return not _NOT_DOC_DATE.search(before or "")


def money_value(v):
    try:
        return float(re.sub(r"[^\d.\-]", "", v or "") or "x")
    except ValueError:
        return None


# Dates that are rightly in the future: a loan's maturity, the next payment's due date.
FUTURE_OK = {"maturity_date", "payment_due_date"}


def suspicious(field, ftype, value, today=None):
    """Why a value looks wrong (for the preview), or None. Checks shape only; the user decides. Dates are judged
    against today, by default the configured one (config.CFG.today)."""
    today = today or CFG.today()
    v = value or ""
    if ftype == "org_name":
        if len(v) > 40:
            return "longer than 40 characters"
        m = _VENDOR_JUNK.search(v)
        if m:
            return f"contains \"{m.group(0).lower()}\""
    if field == "invoice_number" and not invoice_number_ok(v):
        return "just a year or a short number" if re.fullmatch(r"\d{1,4}", v.strip()) else "no letter, dash or 6+ digits"
    if ftype == "date":
        try:
            d = dt.date.fromisoformat(v)
        except ValueError:
            return "not a date"
        if field in FUTURE_OK:
            if not dt.date(2000, 1, 1) <= d <= today + dt.timedelta(days=366 * 10):
                return "outside 2000 to ten years from today"
        elif not dt.date(2000, 1, 1) <= d <= today + dt.timedelta(days=60):
            return "outside 2000 to 60 days from today"
    if ftype == "money" and field in ("total", "amount", "amount_due"):
        n = money_value(v)
        if n is not None and n <= 0:
            return "zero or negative"
    return None


def document_flags(recs, today=None):
    """{index into recs: reason} for one document's records: per-value checks plus currency mismatches."""
    flags = {}
    for i, r in enumerate(recs):
        why = suspicious(r["field"], r.get("type") or "", r["value"], today)
        if why:
            flags[i] = why
    cur = [i for i, r in enumerate(recs) if r["field"] == "currency"]
    syms = {_SYMBOL[c] for r in recs if r.get("type") == "money" for c in r["value"] if c in _SYMBOL}
    for i in cur:
        if syms and recs[i]["value"] not in syms:
            flags.setdefault(i, f"currency does not match the amounts ({', '.join(sorted(syms))})")
    if len(syms) > 1:
        for i, r in enumerate(recs):
            if r.get("type") == "money":
                flags.setdefault(i, "amounts in more than one currency")
    return flags


def sensitive(value, label=""):
    """The detector kind if value (read alone or after its label) looks sensitive, else None."""
    for text in (value, f"{label}: {value}" if label else None):
        if text:
            spans = detect.find_spans(text)
            if spans:
                return spans[0][2]
    return None


def _typed(ftype, text):
    """[(start, end, value)] of typed values in text."""
    if ftype in WHOLE:
        s = text.strip().lstrip(":").strip()  # "From: Example Co": the label's colon is not the value
        if s and valid_value(ftype, s):
            off = text.index(s)
            return [(off, off + len(s), s)]
        return []
    return [(m.start(), m.end(), m.group(0)) for m in _RX[ftype].finditer(text)]


def _cells(text, base=0):
    """[(start, end, text)] cells split on two or more spaces, tabs or ' | '."""
    out, pos = [], 0
    for m in CELL_SPLIT.finditer(text):
        if m.start() > pos:
            out.append((base + pos, base + m.start(), text[pos:m.start()]))
        pos = m.end()
    if pos < len(text):
        out.append((base + pos, base + len(text), text[pos:]))
    return [c for c in out if c[2].strip()]


# ---------------------------------------------------------------- the engine
class Extraction:
    def __init__(self, prof):
        self.prof = prof
        self.allowed = allowed_fields(prof)
        self.values = {}      # (group, plan, row, field) -> record
        self.dropped = {}     # kind -> count
        self.plans = {}       # group -> [plan names]
        # document dates (when the money moved): never a renewal or expiry date, never more than 60 days ahead
        self.doc_dates = {(f["group"], f["name"]) for f in prof.get("fields", []) if f.get("document_date")}
        self.rows = {}        # field name -> rows taken so far ("each" fields)

    def offer(self, group, plan, row, field, ftype, value, page, label, src, span=None, spans=(), rank=0, order=0):
        if (group, field) not in self.allowed:
            return  # allowlist: never a field the profile doesn't name
        if span and any(span[0] < e and s < span[1] for s, e, _, _ in spans):
            k = next(k for s, e, k, _ in spans if span[0] < e and s < span[1])
            self.dropped[k] = self.dropped.get(k, 0) + 1
            return
        value = normalize(ftype, value)
        if not value or (field in VALIDATORS and not VALIDATORS[field](value)):
            return
        if (group, field) in self.doc_dates and not plausible_document_date(value):
            rank += 200  # a far-future date only when no nearer one is found (the preview flags it)
        kind = sensitive(value, label)
        if kind:
            self.dropped[kind] = self.dropped.get(kind, 0) + 1
            return
        key = (group, plan, row, field)
        cur = self.values.get(key)
        if cur and (cur["_rank"], cur["_order"]) <= (rank, order):
            return
        self.values[key] = {"group": group, "plan": plan, "row": row, "field": field, "value": value,
                            "file": src, "page": page, "label": label, "_rank": rank, "_order": order,
                            "_span": list(span) if span else None}

    def records(self, keep_span=False):
        """Records without internals; keep_span adds "span", the value's character offsets in its page's text
        (None when it came from no single place, such as a known sender or the dollar-sign currency)."""
        recs = [{**{k: v for k, v in r.items() if not k.startswith("_")}, **({"span": r["_span"]} if keep_span else {})}
                for r in self.values.values()]
        recs = [r for r in recs if _requires_ok(self.prof, r, recs)]
        return sorted(recs, key=lambda r: (r["group"], r["plan"] or "", r["row"] or 0, r["field"]))


def _requires_ok(prof, rec, recs):
    f = next((f for f in prof.get("fields", []) if f["group"] == rec["group"] and f["name"] == rec["field"]), None)
    if not f or not f.get("requires"):
        return True
    for field, allowed in f["requires"].items():
        have = {r["value"] for r in recs if r["field"] == field}
        if not have & set(allowed):
            return False
    return True


def _group_of_cell(prof, cell):
    low = cell.lower()
    for g, spec in prof.get("groups", {}).items():
        for w in spec.get("words", []):
            if _label_rx(w).search(low):
                return g
    return None


def _heading(prof, line, fields):
    """[(group, plan or None)] when line is a section or plan-column header, else None."""
    if not any(spec.get("per_plan") for spec in prof.get("groups", {}).values()):
        return None
    if _RX["money"].search(line) or _RX["percent"].search(line) or len(line.strip()) > 140:
        return None
    out = []
    for _, _, cell in _cells(line):
        c = " ".join(cell.split())
        if len(c) > 60 or len(c.split()) > 8 or _STOP.match(c):
            continue
        g = _group_of_cell(prof, c)
        if not g:
            continue
        spec = prof["groups"][g]
        markers = [m for m in spec.get("plan_markers", []) if _label_rx(m).search(c)]
        if spec.get("per_plan") and markers and valid_value("plan_name", c):
            out.append((g, c))
        else:
            out.append((g, None))
    if not out:
        return None
    named = [x for x in out if x[1]]
    return named or out[:1]


def extract(prof, pages, src, keep_span=False):
    """pages: [(page number, raw text)]. Returns (records, dropped counts, plans). keep_span: each record also
    carries "span" (see Extraction.records)."""
    ex = Extraction(prof)
    fields = _compile(prof)
    groups = prof.get("groups", {})
    default = prof.get("default_group") or next(iter(groups))
    order = 0
    current = [(default, None)]
    for pno, text in pages:
        spans = detect.find_spans(text)
        lines = text.split("\n")
        offs, o = [], 0
        for ln in lines:
            offs.append(o)
            o += len(ln) + 1
        cuts = _column_cuts(fields, lines)
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            h = _heading(prof, line, fields)
            if h:
                current = h
                for g, p in h:
                    if p and p not in ex.plans.setdefault(g, []):
                        ex.plans[g].append(p)
                        ex.offer(g, p, None, "plan_name", "plan_name", p, pno, "plan heading", src, rank=0, order=order)
                        m = _RX["plan_type"].search(p)
                        if m:
                            ex.offer(g, p, None, "plan_type", "plan_type", m.group(0), pno, "plan heading", src,
                                     rank=0, order=order)
                continue
            order += 1
            _line(ex, fields, groups, current, lines, offs, i, pno, src, spans, order, cuts)
        for t in prof.get("tables", []):
            _table(ex, t, lines, offs, pno, src, spans)
    for f in fields:
        if f.get("anywhere"):
            for pno, text in pages:
                spans = detect.find_spans(text)
                hit = _typed(f["type"], text)
                if hit and f["type"] == "date":  # not a renewal or expiry date, not far ahead, when one exists
                    hit = [h for h in hit if plausible_document_date(parse_date(h[2]) or "", text[max(0, h[0] - 40):h[0]])] \
                        or hit
                    if f["anywhere"] == "earliest":  # a contract's date comes before its first payment's
                        hit = sorted(hit, key=lambda h: parse_date(h[2]) or "9999")
                if hit:
                    s, e, v = hit[0]
                    ex.offer(f["group"], None, None, f["name"], f["type"], v, pno, "anywhere", src, (s, e), spans,
                             rank=99, order=0)
                    break
            if f["type"] == "currency" and not any(r["field"] == f["name"] for r in ex.values.values()):
                if any("$" in t for _, t in pages):
                    ex.offer(f["group"], None, None, f["name"], "currency", "USD", pages[0][0], "dollar sign", src,
                             rank=100)
        if f.get("known_vendor"):
            kv = known_vendor(pages, f["known_vendor"] if isinstance(f["known_vendor"], str) else None)
            if kv:
                ex.offer(f["group"], None, None, f["name"], f["type"], kv[0], kv[1], kv[2], src, rank=-10)
        if f.get("range") is not None:
            _range(ex, f, pages, src)
        if f.get("link_month") is not None:
            _link_month(ex, f, pages, src)
        if f.get("from") == "top":
            for pno, text in pages[:1]:
                for ln in text.split("\n")[:8]:
                    cells = _cells(ln)
                    s = trim_vendor(cells[0][2]) if cells else ""
                    if valid_value(f["type"], s) and not re.search(
                            r"receipt|invoice|welcome|thank|order|transaction|store\s*#|tel|phone|www\.|http|page"
                            r"|^(?:sub\s*-?\s*)?total\b|^amount\b|^balance\b|^tax\b|^date\b|^bill\s+to\b",
                            s, re.I):
                        ex.offer(f["group"], None, None, f["name"], f["type"], s.title() if s.isupper() else s,
                                 pno, "top of page", src, rank=98)
                        break
    for f in fields:
        if f.get("name_date"):
            _name_date(ex, f, pages, src)
    for f in fields:  # "model_from": the model is what follows the make in a "Make and Model" value
        src_field = f.get("model_from")
        if src_field and not any(r["field"] == f["name"] for r in ex.values.values()):
            mm = next((r for r in ex.values.values() if r["field"] == src_field), None)
            mk = re.match(r"(" + TYPES[MODEL_PREFIX] + r")\s+(.+)", mm["value"], re.I) if mm and MODEL_PREFIX else None
            if mk:
                ex.offer(f["group"], None, None, f["name"], f["type"], mk.group(2), mm["page"], mm["label"],
                         mm["file"], rank=mm["_rank"] + 1, order=mm["_order"])
    recs = ex.records(keep_span)
    if not any(r["label"] not in ("anywhere", "top of page", "dollar sign", "known sender") for r in recs):
        recs = []  # nothing matched a label: this document doesn't fit the profile, so nothing is taken from it
    return recs, ex.dropped, ex.plans


_LINK_MONTH = re.compile(r"[?&#/]year=(\d{4})&month=(\d{1,2})(?!\d)|[?&#/]month=(\d{1,2})&year=(\d{4})(?!\d)", re.I)
_NAME_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?=\s|_|-|\.|$)")


def _link_month(ex, f, pages, src):
    """The month a link's query names (year=2030&month=9): its first day for "link_month" 0, its last for 1."""
    for pno, text in pages:
        for m in _LINK_MONTH.finditer(text):
            y, mo = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            y, mo = int(y), int(mo)
            if not 1 <= mo <= 12:
                continue
            day = dt.date(y, mo, 1) if f["link_month"] == 0 else \
                dt.date(y + mo // 12, mo % 12 + 1, 1) - dt.timedelta(days=1)
            ex.offer(f["group"], None, None, f["name"], "date", day.isoformat(), pno, "month in a link", src,
                     None, (), rank=50, order=0)
            return


def _name_date(ex, f, pages, src):
    """The YYYY-MM-DD at the start of the file name, when nothing in the text gave this field a value."""
    if any(r["group"] == f["group"] and r["field"] == f["name"] for r in ex.values.values()):
        return
    m = _NAME_DATE.match(os.path.basename(src or ""))
    if m and parse_date(m.group(1)) and pages:
        ex.offer(f["group"], None, None, f["name"], "date", m.group(1), pages[0][0], "file name", src, rank=150)


def _range(ex, f, pages, src):
    """A labeled date range ("billing period October 1 - October 31, 2026"): f["range"] 0 takes the start (with
    the end's year when it has none), 1 the end. Preferred over the per-line value, which can't see the range."""
    labs = [rx for _, _, _, rx in f["_labels"]]
    for pno, text in pages:
        for rx in labs:
            for m in rx.finditer(text):
                r = DATE_RANGE.search(text, m.end(), m.end() + 80)
                if not r or "\n\n" in text[m.end():r.start()]:
                    continue
                end = parse_date(r.group(3))
                if not end:
                    continue
                start = parse_date(f"{r.group(1)}, {r.group(2) or end[:4]}")
                if start and start > end:  # December 1 - January 31, 2027
                    start = parse_date(f"{r.group(1)}, {int(end[:4]) - 1}")
                val, span = (start, r.span(1)) if f["range"] == 0 else (end, r.span(3))
                if val:
                    spans = detect.find_spans(text)
                    ex.offer(f["group"], None, None, f["name"], "date", val, pno, m.group(0).lower(), src, span,
                             spans, rank=-1, order=0)
                    return


def _hits(fields, groups, current, line):
    """Label hits on a line: [(start, end, [(field, label, idx, rank)])], overlaps resolved (longest wins)."""
    cur_groups = {g for g, _ in current}
    raw = []
    for f in fields:
        g = f["group"]
        if not (g in cur_groups or groups.get(g, {}).get("global")):
            continue
        for rank, text, idx, rx in f["_labels"]:
            for m in rx.finditer(line):
                raw.append((m.start(), m.end(), f, text, idx, rank))
    raw.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    by_start = {}
    for s, e, f, text, idx, rank in raw:
        fs = by_start.setdefault(s, {})
        if id(f) not in fs:  # each field's longest label at this position
            fs[id(f)] = (e, (f, text, idx, rank))
    spans = []
    for s in sorted(by_start):
        e = max(v[0] for v in by_start[s].values())
        if any(s < se and ss < e for ss, se, _ in spans):
            continue  # starts inside a longer label already taken
        # an exclusive_label field claims only its own whole label: "invoice" never what follows "invoice date"
        spans.append((s, e, [v[1] for v in by_start[s].values() if v[0] == e or not v[1][0].get("exclusive_label")]))
    spans.sort()
    kept = []
    for sp in spans:
        gap = line[kept[-1][1]:sp[0]] if kept else ""
        if kept and (not re.search(r"\s{2,}|\t|\|", gap) or _RX["money"].search(gap) or _RX["percent"].search(gap)):
            continue  # inside a running label or a value ("20% after deductible"), not a new column
        kept.append(sp)
    return kept


def _column_cuts(fields, lines):
    """{field name: column} for "column_before" fields whose header is on this page."""
    out = {}
    for f in fields:
        h = f.get("column_before")
        if not h:
            continue
        text, nth = (h, 0) if isinstance(h, str) else (h[0], h[1])
        rx = _label_rx(text)
        for ln in lines:
            ms = list(rx.finditer(ln))
            if len(ms) > nth:
                out[f["name"]] = ms[nth].start()
                break
    return out


# After a cell-start label: an optional "(Name and Address)", then a colon, a column gap or the end of the line.
_LABEL_END = re.compile(r"(?:\s*\([^)\n]{0,40}\))?(?:\s*:|\s{2,}|\t|\s*$)")


def _line(ex, fields, groups, current, lines, offs, i, pno, src, spans, order, cuts=None):
    line = lines[i]
    hits = _hits(fields, groups, current, line)
    if not hits:
        return
    plans = [p for _, p in current]
    for n, (s, e, fl) in enumerate(hits):
        full_end = hits[n + 1][0] if n + 1 < len(hits) else len(line)
        for f, label, idx, rank in fl:
            ftype = f["type"]
            if f.get("label_at_cell_start") and idx != "line" and line[:s].strip() and not re.search(r"(?:\s{2,}|\t|\|)\s*$", line[:s]):
                continue  # "from" inside a sentence ("... the charges shown on this"), not a label
            if f.get("label_at_cell_start") and idx != "line" and not _LABEL_END.match(line[e:]):
                continue  # a label is followed by a colon, a column gap or nothing (the value below)
            end = full_end
            own_line = idx == "line"
            if own_line:  # [label, "line"]: the first value in the rest of the line, past other labels
                end, idx = len(line), 0
            if cuts and f["name"] in cuts:
                if s >= cuts[f["name"]]:
                    continue  # the label itself sits in a cut-off column
                end = max(e, min(end, cuts[f["name"]]))
            seg = line[e:end]
            if f.get("before") or idx == "before":
                lo = hits[n - 1][1] if n else 0
                vals = _typed(ftype, line[lo:s])
                if vals:
                    s2, e2, v = vals[-1]
                    ex.offer(f["group"], None, None, f["name"], ftype, v, pno, label, src,
                             (offs[i] + lo + s2, offs[i] + lo + e2), spans, rank, order)
                continue
            row = None
            if f.get("each"):
                row = ex.rows.get(f["name"], 0) + 1
            first = min([c[0] - offs[i] for c in _cells(seg, offs[i] + e) if _typed(ftype, c[2])] or [len(line)])
            if any(nx.search(line[s:first]) for nx in f["_not"]):
                continue
            if f.get("document_date") and _RENEWAL.search(line[max(0, s - 30):first]):
                continue  # "Renewal date", "Plan expires": not when the money moved
            g = f["group"]
            per_plan = groups.get(g, {}).get("per_plan")
            targets = plans if per_plan else [None]
            cells = [c for c in _cells(seg, offs[i] + e) if _typed(ftype, c[2])]
            if not cells and ftype not in WHOLE and _typed(ftype, seg):
                cells = [(offs[i] + e, offs[i] + end, seg)]
            got = False
            if cells:
                if len(targets) > 1 and len(hits) == 1:
                    if len(cells) == len(targets):
                        for plan, c in zip(targets, cells):
                            _take(ex, g, plan, f, ftype, c, idx, pno, label, src, spans, rank, order)
                    got = True
                else:
                    plan = targets[0] if per_plan else None
                    if idx in ("current", "ytd", "last"):
                        vals = [(c[0] + s2, c[0] + e2, v) for c in cells for s2, e2, v in _typed(ftype, c[2])]
                        pick = None
                        if idx == "current":
                            pick = vals[-2] if len(vals) >= 2 else vals[0]
                        elif idx == "last" and vals:
                            pick = vals[-1]
                        elif len(vals) >= 2:
                            pick = vals[-1]
                        if pick:
                            ex.offer(g, plan, row, f["name"], ftype, pick[2], pno, label, src, pick[:2], spans,
                                     rank, order)
                        got = True
                    else:
                        got = _take(ex, g, plan, f, ftype, cells[0], idx, pno, label, src, spans, rank, order,
                                    row)
            # One-line form rows (each, column_before) never read a value from the lines below: a blank row's
            # value would come from the next row.
            if not got and own_line:
                _near(ex, fields, groups, current, g, f, ftype, lines, offs, i, pno, label, src, spans, rank, order)
            elif not got and not (len(targets) > 1) and not (f.get("each") or f.get("column_before")):
                _below(ex, g, (targets[0] if per_plan else None), f, ftype, lines, offs, i, s, end, idx, pno,
                       label, src, spans, rank, order, row)
            if row and (g, None, row, f["name"]) in ex.values:
                ex.rows[f["name"]] = row


def _near(ex, fields, groups, current, g, f, ftype, lines, offs, i, pno, label, src, spans, rank, order):
    """A [label, "line"] label with no value on its line: an e-signature stamp ("DocuSigned by:", the signer's
    name, an envelope ID) is taller than one line of text, so the date beside the signature lands on a line just
    above or below the label's. Take the nearest value within two non-empty lines either way, never on or past a
    line holding another "line" label (the seller's signature line, below the buyer's), and never one that a "not"
    or renewal word precedes on its line. The value outranks the next label's ("seller signs" after "buyer signs")
    but not this label's own value on its line."""
    if ftype in WHOLE:
        return  # free text from a neighbouring line would be a guess

    def stop(j):
        return any(idx == "line" for _, _, fl in _hits(fields, groups, current, lines[j]) for _, _, idx, _ in fl)

    found = []
    for step in (-1, 1):
        j, seen = i + step, 0
        while 0 <= j < len(lines) and seen < 2:
            if lines[j].strip():
                if stop(j):
                    break
                seen += 1
                for c in _cells(lines[j], offs[j]):
                    vals = _typed(ftype, c[2])
                    if not vals:
                        continue
                    before = lines[j][:c[0] - offs[j] + vals[0][0]]
                    if any(nx.search(before) for nx in f["_not"]) or \
                            (f.get("document_date") and _RENEWAL.search(before)):
                        continue
                    found.append((seen, step, c))
                    break
                if found and found[-1][1] == step:
                    break
            j += step
    if found:
        _, _, c = min(found, key=lambda x: (x[0], x[1]))  # nearest; above first on a tie (the stamp's top line)
        _take(ex, g, None, f, ftype, c, 0, pno, label, src, spans, rank + 0.5, order)


def _take(ex, g, plan, f, ftype, cell, idx, pno, label, src, spans, rank, order, row=None):
    vals = _typed(ftype, cell[2])
    if not isinstance(idx, int) or idx >= len(vals):
        return False
    s, e, v = vals[idx]
    ex.offer(g, plan, row, f["name"], ftype, v, pno, label, src, (cell[0] + s, cell[0] + e), spans, rank, order)
    return True


def _below(ex, g, plan, f, ftype, lines, offs, i, start, end, idx, pno, label, src, spans, rank, order, row=None):
    if idx in ("ytd", "last", "before") or (isinstance(idx, int) and idx > 0):
        return
    lo, hi = max(0, start - 4), max(end, start + 24) + 4
    seen, most = 0, f.get("below_lines", 3)
    for j in range(i + 1, min(len(lines), i + most + 3)):
        win = lines[j][lo:hi]
        if not lines[j].strip():
            continue
        seen += 1
        if win.strip():
            cells = [c for c in _cells(win, offs[j] + lo) if _typed(ftype, c[2])]
            if not cells and ftype not in WHOLE and _typed(ftype, win):
                cells = [(offs[j] + lo, offs[j] + lo + len(win), win)]
            if cells and f.get("below_values_only") and re.search(r"[A-Za-z]{2,}", _RX[ftype].sub("", win)):
                cells = []  # a figure inside the box's sentence ("including your down payment of $500.00")
            if cells:
                _take(ex, g, plan, f, ftype, cells[0], 0, pno, label, src, spans, rank + 50, order, row)
                return
            if ftype in WHOLE:
                return  # the first line below is the value or nothing: never read further down (addresses)
        if seen >= most:
            return


def _table(ex, t, lines, offs, pno, src, spans):
    cols = [(c, _label_rx(lab)) for c in t["columns"] for lab in c["labels"]]
    row = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        found = {}
        for c, rx in cols:
            m = rx.search(line)
            if m and c["name"] not in found and not any(m.start() < fe and fs < m.end() for fs, fe in found.values()):
                found[c["name"]] = (m.start(), m.end())
        if len(found) < t.get("header_min", 3):
            i += 1
            continue
        centers = {n: (s + e) / 2 for n, (s, e) in found.items()}
        spec = {c["name"]: c for c in t["columns"]}
        blank = 0
        j = i + 1
        while j < len(lines) and blank < 2:
            ln = lines[j]
            if not ln.strip():
                blank += 1
                j += 1
                continue
            blank = 0
            cells = _cells(ln, 0)
            got = {}
            for cs, ce, ctext in cells:
                name = min(centers, key=lambda n: abs(centers[n] - (cs + ce) / 2))
                c = spec[name]
                v = " ".join(ctext.split())
                if name not in got and valid_value(c["type"], v):
                    got[name] = (cs, ce, v)
            req = t.get("row_requires", [])
            if not got or (req and not any(r in got for r in req)):
                break
            row += 1
            for name, (cs, ce, v) in got.items():
                ex.offer(t["group"], None, row, name, spec[name]["type"], v, pno, "table: " + name, src,
                         (offs[j] + cs, offs[j] + ce), spans, 0, row)
            j += 1
        i = j
