"""Sensitive-value detectors and redaction for document intake. Stdlib only.

Every detector reports spans (start, end, kind, tag); callers report counts per kind, never the values.
Checksums cut false positives: card numbers must pass Luhn and a known issuer prefix, routing numbers the
ABA 3-7-1 checksum and a Federal Reserve prefix. Context detectors (account, DOB, passport, driver's license,
member and employee IDs, contract and deal numbers) need their keyword shortly before the value. Vehicle
identification numbers (VINs) are 17 characters without I, O or Q, with at least one letter and one digit, and must pass the
check digit in position 9 (49 CFR 565.15); after a "VIN" label any VIN-shaped token counts (an OCR misread).

Phone numbers are US formats with separators or parentheses (bare digit runs only after a Phone/Tel/Mobile/Cell
label); street addresses are the line after an "Address:" or "Street:" label, or number + street + suffix followed
by a town and state. Redaction is pattern-based and can miss things: names are not detected, other address and
phone layouts slip through, and OCR errors can break a number so no pattern matches it.
"""
import re
from collections import Counter

# Kinds that make a file secret material: it is held, never extracted.
HOLD_KINDS = ("private-key", "api-key", "2fa", "password", "pin", "secret")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_PAT = (r"(?:\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})|\d{4}-\d{1,2}-\d{1,2}"
            r"|(?i:" + _MONTH + r")\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+(?i:" + _MONTH + r")\s+\d{4})")

# ---------------------------------------------------------------- credentials (held)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
                          r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----)?")
_API_KEY = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}"
                      r"|sk_(?:live|test)_[A-Za-z0-9]{16,}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}"
                      r"|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,})")
_OTP = re.compile(r"otpauth(?:-migration)?://\S+|(?i:\b(?:backup|recovery)\s+codes?\b)")
_PASSWORD = re.compile(r"(?i:\b(?:password|passwd|passcode|pass\s?phrase|pwd)\b)\s*[:=]\s*"
                       r"(?=[^\s_]*[A-Za-z0-9])[^\s_]\S{3,}")
_PIN = re.compile(r"\b(?:PIN|Pin|pin)(?:\s*(?:code|number|#))?\s*(?:is\s+)?[:=#]?\s*\d{4,8}\b")
_SECRET = re.compile(r"(?i:\b(?:secret(?:\s+key)?|client\s+secret|api[\s_-]?key|access[\s_-]?key"
                     r"|auth(?:entication)?\s+token|token|recovery\s+key|seed\s+phrase)\b)\s*[:=]\s*\S{8,}")

# ---------------------------------------------------------------- identifiers
_SSN_DASHED = re.compile(r"(?<![\d-])(\d{3})-(\d{2})-(\d{4})(?![\d-])")
_SSN_CTX = re.compile(r"(?i:\b(?:ssn|social\s+security(?:\s+(?:number|no\.?))?|soc\.?\s*sec\.?|itin"
                      r"|taxpayer\s+identification\s+number|tin)\b)\D{0,80}?(?<!\d)(\d{3})[ -]?(\d{2})[ -]?(\d{4})(?!\d)")
# Card candidates: 13+ digits with single space, dash or dot separators, mixed or not. A dot counts as the edge of
# a value only when no digit sits on its other side, so a decimal or a version string isn't cut into a card.
_CARD = re.compile(r"(?<![\d-])(?<!\d\.)\d(?:[ .-]?\d){12,40}(?![\d-])(?!\.\d)")
# Card groupings: 4-4-4-1..4, 4-4-4-4-1..3 (19 digits), Amex 4-6-5 and Diners 4-6-4. A candidate with a dot must be
# grouped like this, which keeps thousands (1.234.567), versions, IP addresses, dates and decimals out.
_CARD_GROUPS = re.compile(r"^(?:\d{13,19}|\d{4}[ .-]\d{4}[ .-]\d{4}[ .-]\d{1,4}(?:[ .-]\d{1,3})?"
                          r"|\d{4}[ .-]\d{6}[ .-]\d{4,5})$")
# Card-shaped digits that fail Luhn, one separator throughout: hidden as long-number (maybe an OCR misread).
_CARD_GROUPED = re.compile(r"^\d{4}([ .-]?)(?:\d{4}\1\d{4}\1(?:\d{1,7}|\d{4}\1\d{1,3})|\d{6}\1\d{4,5})$")
_CARD_PIECE = re.compile(r"\d+")


def _card_spans(text):
    """[(start, end, kind)] for card candidates. A run of groups (a CSV row with an expiry, a list number before
    the card) is cut to the longest stretch of whole groups that is a card."""
    out = []
    for m in _CARD.finditer(text):
        raw = m.group(0)
        if "." not in raw and is_card(re.sub(r"\D", "", raw)):
            out.append((m.start(), m.end(), "card"))
            continue
        groups = [(g.start(), g.end()) for g in _CARD_PIECE.finditer(raw)]
        best = None
        for i in range(len(groups)):
            for j in range(len(groups), i, -1):
                piece = raw[groups[i][0]:groups[j - 1][1]]
                digits = re.sub(r"\D", "", piece)
                if best and len(digits) <= best[2]:
                    break
                if 13 <= len(digits) <= 19 and _CARD_GROUPS.match(piece) and is_card(digits):
                    best = (groups[i][0], groups[j - 1][1], len(digits))
        if best:
            out.append((m.start() + best[0], m.start() + best[1], "card"))
            continue
        n = len(re.sub(r"\D", "", raw))
        if n >= 13 and _CARD_GROUPED.match(raw) and ("." not in raw or n >= 15):
            out.append((m.start(), m.end(), "long-number"))
    return out


_NINE = re.compile(r"(?<![\d-])\d{9}(?![\d-])")
_EIN = re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])")
_MBI = re.compile(r"\b[1-9][AC-HJKMNP-RT-Y][0-9AC-HJKMNP-RT-Y]\d-?[AC-HJKMNP-RT-Y][0-9AC-HJKMNP-RT-Y]\d-?"
                  r"[AC-HJKMNP-RT-Y]{2}\d{2}\b")
_MRZ = re.compile(r"^(?=[A-Z0-9<]*<<)[A-Z0-9<]{30,44}$", re.M)
_ACCOUNT = re.compile(r"(?i:\b(?:account|acct|a/c)\b)\D{0,60}?(?<![\d.,/$])(\d(?:[ -]?\d){5,16})(?![\d,/]|\.\d)")
_DOB = re.compile(r"(?i:\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s?date|birthday|born(?:\s+on)?)\b)"
                  r"[^\n\d]{0,40}?\n?[^\n\d]{0,40}?(" + DATE_PAT + ")")
_PASSPORT = re.compile(r"(?i:\bpassport\b)\D{0,60}?\b((?=[A-Z0-9]*\d)[A-Z0-9]{6,9})\b")
_DL = re.compile(r"(?i:\b(?:driver'?s?\s+licen[cs]e|driving\s+licen[cs]e|DLN|DL|lic(?:ense)?\.?\s*(?:no|number|#))"
                 r"(?![A-Za-z]))\D{0,60}?\b((?=(?:[A-Z-]*\d){5})[A-Z]{0,2}\d[A-Z0-9-]{4,16})\b")
_ID_TOKEN = r"\b((?=(?:[A-Za-z-]*\d){3})[A-Za-z0-9][A-Za-z0-9-]{3,24})\b"
_MEMBER = re.compile(r"(?i:\b(?:member|subscriber|group|policy|insured|enrollee|beneficiary|certificate)'?s?\s*"
                     r"(?:id|identification|#|number|no\.?|num)(?![A-Za-z]))\W{0,40}?" + _ID_TOKEN)
_EMPLOYEE = re.compile(r"(?i:\b(?:employee|emp\.?|associate|personnel|badge|worker|staff)'?s?\s*"
                       r"(?:id|identification|#|number|no\.?|num)(?![A-Za-z]))\W{0,40}?" + _ID_TOKEN)
_CONTRACT = re.compile(r"(?i:\b(?:contract|deal|stock|loan|lease|r\.?o\.?s\.?|buyer'?s?\s+order)\s*"
                       r"(?:id|#|number|no\.?|num)(?![A-Za-z]))\W{0,40}?" + _ID_TOKEN)
_VIN = re.compile(r"(?<![A-Za-z0-9])(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}"
                  r"(?![A-Za-z0-9])")
_VIN_LABELED = re.compile(r"(?i:\b(?:vin|vehicle\s+identification\s+(?:number|no\.?|#))(?![A-Za-z]))\W{0,40}?"
                          r"(?<![A-Za-z0-9])((?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-HJ-NPR-Z0-9]{17})(?![A-Za-z0-9])")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_LONG = re.compile(r"(?<!\d)\d{10,}(?!\d)")
# US phone numbers: separators or parentheses required, NANP area code and exchange start 2-9
_PHONE = re.compile(r"(?<![\w+-])(?:\+?1[\s.-]?)?(?:\([2-9]\d{2}\)\s?|[2-9]\d{2}[\s.-])[2-9]\d{2}[\s.-]\d{4}(?![\d-])")
_PHONE_LABELED = re.compile(r"(?i:\b(?:phone|tel|telephone|mobile|cell)\b)\W{0,20}?((?:\+?1)?[2-9]\d{2}[2-9]\d{6})(?!\d)")
_SUFFIX = (r"(?:Dr|St|Ave|Rd|Ln|Ct|Blvd|Way|Pl|Cir|Ter|Pkwy|Drive|Street|Avenue|Road|Lane|Court|Boulevard|Place"
           r"|Circle|Terrace|Parkway)")
_STREET = re.compile(r"(?<![\w-])\d{1,6}[A-Z]?[ \t]+(?:[NSEW]\.?[ \t]+)?(?:[A-Z0-9][A-Za-z0-9.'-]*[ \t]+){1,4}" + _SUFFIX
                     + r"\b\.?(?:[ \t]+(?:[NSEW]|North|South|East|West|NE|NW|SE|SW))?(?:,?[ \t]+(?:Apt|Unit|Ste|Suite|#)\.?[ \t]*"
                     r"[A-Za-z0-9-]+)?,?[ \t]*\n?[ \t]*[A-Z][A-Za-z .'-]{1,40},\s*[A-Z]{2}\b(?:\s+\d{5}(?:-\d{4})?)?")
_ADDRESS_LABEL = re.compile(r"(?i:(?<![A-Za-z-])(?<!e-mail )(?<!email )(?<!ip )(?<!web )(?:address|street)\s*:)"
                            r"[ \t]*([^\n]*)")
_CITY_STATE = re.compile(r"^[ \t]*[A-Z][A-Za-z .'-]{1,40},\s*[A-Z]{2}\b(?:\s+\d{5}(?:-\d{4})?)?[ \t]*$")

# Email transport headers carry relay IDs, timestamps and hashes, never a bank routing number.
_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,60}):", re.M)
_TRANSPORT = re.compile(r"(?i)^(?:received|received-spf|x-[a-z0-9-]+|arc-[a-z-]+|dkim-[a-z-]+|authentication-results"
                        r"|message-id|references|in-reply-to|return-path|delivered-to|feedback-id|list-[a-z-]+)$")


def transport_header_spans(text):
    """[(start, end)] of an email's transport headers, when text starts with an RFC 5322 header block."""
    if not _HEADER_LINE.match(text):
        return []
    m = re.search(r"\r?\n\r?\n", text)
    end = m.start() if m else len(text)
    out, cur = [], None
    for h in _HEADER_LINE.finditer(text, 0, end):
        if cur:
            out.append((cur, h.start()))
            cur = None
        if _TRANSPORT.match(h.group(1)):
            cur = h.start()
    if cur is not None:
        out.append((cur, end))
    return out


ITIN_GROUPS = set(range(50, 66)) | set(range(70, 89)) | {90, 91, 92} | set(range(94, 100))
ABA_PREFIXES = set(range(0, 13)) | set(range(21, 33)) | set(range(61, 73)) | {80}


def ssn_kind(area, group, serial):
    """'ssn', 'itin' or None for a 3-2-4 number."""
    if area in ("000", "666") or group == "00" or serial == "0000":
        return None
    if area[0] == "9":
        return "itin" if int(group) in ITIN_GROUPS else None
    return "ssn"


def luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def card_prefix_ok(d):
    n = len(d)
    if d[0] == "4":
        return n in (13, 16, 19)
    if 51 <= int(d[:2]) <= 55 or 2221 <= int(d[:4]) <= 2720:
        return n == 16
    if d[:2] in ("34", "37"):
        return n == 15
    if d.startswith("6011") or d.startswith("65") or 644 <= int(d[:3]) <= 649 or d[:2] == "62":
        return 16 <= n <= 19
    if 3528 <= int(d[:4]) <= 3589:
        return 16 <= n <= 19
    if d[:2] in ("36", "38", "39") or 300 <= int(d[:3]) <= 305:
        return 14 <= n <= 19
    return False


def is_card(digits):
    return 13 <= len(digits) <= 19 and card_prefix_ok(digits) and luhn_ok(digits)


_VIN_VALUE = {**{str(n): n for n in range(10)},
              **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9,
              **dict(zip("STUVWXYZ", range(2, 10)))}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_ok(v):
    """A 17-character VIN whose check digit (position 9) matches: the weighted sum of the transliterated
    characters mod 11, 10 written X (49 CFR 565.15)."""
    v = (v or "").upper()
    if len(v) != 17 or any(c not in _VIN_VALUE for c in v):
        return False
    r = sum(_VIN_VALUE[c] * w for c, w in zip(v, _VIN_WEIGHTS)) % 11
    return v[8] == ("X" if r == 10 else str(r))


def aba_ok(d):
    if len(d) != 9 or not d.isdigit() or int(d[:2]) not in ABA_PREFIXES:
        return False
    w = (3, 7, 1) * 3
    return sum(int(c) * k for c, k in zip(d, w)) % 10 == 0


# Priority: when two spans overlap, the earlier kind in this list wins.
PRIORITY = ["private-key", "api-key", "2fa", "password", "pin", "secret", "ssn", "itin", "card", "medicare",
            "passport", "vin", "ein", "routing", "account", "contract-id", "dob", "drivers-license", "member-id",
            "employee-id", "email", "phone", "address", "long-number"]

TAGS = {"ssn": "[SSN]", "itin": "[ITIN]", "routing": "[ROUTING]", "dob": "[DOB]", "medicare": "[MEDICARE-ID]",
        "passport": "[PASSPORT]", "drivers-license": "[DRIVERS-LICENSE]", "member-id": "[MEMBER-ID]",
        "employee-id": "[EMPLOYEE-ID]", "vin": "[VIN]", "contract-id": "[CONTRACT-ID]", "ein": "[EIN]",
        "email": "[EMAIL]", "phone": "[PHONE]", "address": "[ADDRESS]",
        "long-number": "[NUMBER]"}


def _tag(kind, value=""):
    if kind in HOLD_KINDS:
        return "[SECRET]"
    if kind in ("card", "account"):
        digits = re.sub(r"\D", "", value)
        return f"[{kind.upper()}-last4:{digits[-4:]}]"
    return TAGS[kind]


def raw_spans(text):
    """Every candidate (start, end, kind) before overlap resolution."""
    out = []

    def add(m, kind, group=0):
        out.append((m.start(group), m.end(group), kind))

    for rx, kind in ((_PRIVATE_KEY, "private-key"), (_API_KEY, "api-key"), (_OTP, "2fa"),
                     (_PASSWORD, "password"), (_PIN, "pin"), (_SECRET, "secret"), (_MBI, "medicare"),
                     (_MRZ, "passport"), (_EIN, "ein"), (_EMAIL, "email")):
        for m in rx.finditer(text):
            add(m, kind)
    for m in _SSN_DASHED.finditer(text):
        k = ssn_kind(*m.groups())
        if k:
            add(m, k)
    for m in _SSN_CTX.finditer(text):
        k = ssn_kind(*m.groups())
        if k:
            out.append((m.start(1), m.end(3), k))
    out.extend(_card_spans(text))
    transport = transport_header_spans(text)
    for m in _NINE.finditer(text):
        if aba_ok(m.group(0)) and not any(s <= m.start() < e for s, e in transport):
            add(m, "routing")
    for rx, kind in ((_ACCOUNT, "account"), (_DOB, "dob"), (_PASSPORT, "passport"), (_DL, "drivers-license"),
                     (_MEMBER, "member-id"), (_EMPLOYEE, "employee-id")):
        for m in rx.finditer(text):
            add(m, kind, 1)
    for m in _VIN.finditer(text):
        if vin_ok(m.group(0)):
            add(m, "vin")
    for m in _VIN_LABELED.finditer(text):
        add(m, "vin", 1)
    for m in _CONTRACT.finditer(text):
        add(m, "contract-id", 1)
    for m in _LONG.finditer(text):
        add(m, "long-number")
    for m in _PHONE.finditer(text):
        add(m, "phone")
    for m in _PHONE_LABELED.finditer(text):
        add(m, "phone", 1)
    for m in _STREET.finditer(text):
        add(m, "address")
    for m in _ADDRESS_LABEL.finditer(text):
        col = m.start() - (text.rfind("\n", 0, m.start()) + 1)
        rest = m.group(1)
        gap = re.search(r"\s{2,}|\t", rest)  # a column gap ends the value: the next column is another field
        s, e = m.start(1), m.start(1) + (gap.start() if gap else len(rest))
        if re.search(r"[ \t]{2,}$|\t$", text[m.start():m.start(1)]):
            e = s  # "Address:" then a column gap: what follows is another column's field
        if not text[s:e].strip():  # "Address:" alone: the value is below it, in the same column
            nl = text.find("\n", m.end(1))
            if nl == -1:
                continue
            nl2 = text.find("\n", nl + 1)
            nl2 = len(text) if nl2 == -1 else nl2
            line = text[nl + 1:nl2]
            lo = max(0, col - 4)
            seg = line[lo:]
            cut = re.search(r"\S(\s{2,}|\t)", seg)
            seg = seg[:cut.start() + 1] if cut else seg
            if not seg.strip() or (lo > 0 and line[lo - 1:lo].strip()):
                continue  # nothing below the label, or the text below starts in another column
            s = nl + 1 + lo + (len(seg) - len(seg.lstrip()))
            e = nl + 1 + lo + len(seg.rstrip())
        if re.fullmatch(r"\s*(?:\[[A-Z0-9:-]+\]\s*)+", text[s:e]):
            continue  # already redacted
        nl = text.find("\n", e)
        if nl != -1 and not text[e:nl].strip():  # a town, state line right after the street line belongs to it
            end2 = text.find("\n", nl + 1)
            end2 = len(text) if end2 == -1 else end2
            if _CITY_STATE.match(text[nl + 1:end2]):
                e = end2
        out.append((s, e, "address"))
    return out


def find_spans(text):
    """Non-overlapping spans [(start, end, kind, tag)], sorted by start."""
    rank = {k: i for i, k in enumerate(PRIORITY)}
    cands = sorted(raw_spans(text), key=lambda s: (rank[s[2]], s[0], -(s[1] - s[0])))
    taken = []
    for s, e, k in cands:
        if any(s < te and ts < e for ts, te, _, _ in taken):
            continue
        taken.append((s, e, k, _tag(k, text[s:e])))
    return sorted(taken)


def counts(spans):
    return dict(Counter(k for _, _, k, _ in spans))


def has_credentials(c):
    return any(k in HOLD_KINDS for k in c)


def redact(text, max_passes=3):
    """(redacted text, counts per kind). Re-scans the result so an overlap can't leave a value behind."""
    total = Counter()
    for _ in range(max_passes):
        spans = find_spans(text)
        if not spans:
            break
        total.update(k for _, _, k, _ in spans)
        for s, e, _, tag in reversed(spans):
            text = text[:s] + tag + text[e:]
    return text, dict(total)


def fmt_counts(c):
    return ", ".join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda kv: PRIORITY.index(kv[0])
                                                    if kv[0] in PRIORITY else 99)) or "none"
