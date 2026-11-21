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
import bisect
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
# Card candidates: 13+ digits with single space, dash or dot separators, mixed or not. A separator counts as the
# edge of a value only when no digit sits on its other side: a hyphenated label (ID-4111 ...) or a trailing dash
# still ends the run, while a decimal or a version string isn't cut into a card.
_CARD = re.compile(r"(?<!\d)(?<!\d-)(?<!\d\.)\d(?:[ .-]?\d){12,}(?!\d)(?!-\d)(?!\.\d)")
# Card groupings: 4-4-4-1..4, 4-4-4-4-1..3 (19 digits), Amex 4-6-5 and Diners 4-6-4. A stretch with a dot, or one
# cut out of a longer run, must be grouped like this, which keeps thousands (1.234.567), versions, IP addresses,
# dates and decimals out.
_CARD_GROUPS = re.compile(r"^(?:\d{13,19}|\d{4}[ .-]\d{4}[ .-]\d{4}[ .-]\d{1,4}(?:[ .-]\d{1,3})?"
                          r"|\d{4}[ .-]\d{6}[ .-]\d{4,5})$")
# Card-shaped digits that fail Luhn, one separator throughout: hidden as long-number (maybe an OCR misread).
_CARD_GROUPED = re.compile(r"^\d{4}([ .-]?)(?:\d{4}\1\d{4}\1(?:\d{1,7}|\d{4}\1\d{1,3})|\d{6}\1\d{4,5})$")
_CARD_PIECE = re.compile(r"\d+")


def _longest_stretch(raw, groups, starts, hi, ok):
    """(i, j, digits) of the longest stretch of whole groups i..j (i in starts, j < hi) with 13-19 digits that
    ok(piece, i, j) accepts. A stretch spans at most 19 groups, so this is linear in the run."""
    best = None
    for i in starts:
        n = 0
        for j in range(i, hi):
            n += groups[j][1] - groups[j][0]
            if n > 19:
                break
            if n >= 13 and (best is None or n > best[2]) and ok(raw[groups[i][0]:groups[j][1]], i, j):
                best = (i, j, n)
    return best


def _card_stretch_ok(piece):
    """A stretch of whole groups that is a card: card-grouped, and length, prefix and Luhn pass."""
    return bool(_CARD_GROUPS.match(piece)) and is_card(re.sub(r"\D", "", piece))


def _has_shorter_card(raw, groups, i, j):
    """Whether a stretch from group i ending before group j is a card too (4111 1111 1111 1111 912 passes as a
    19-digit card, and so does its first sixteen): then the groups past the shorter card may be a neighbour's."""
    return any(_card_stretch_ok(raw[groups[i][0]:groups[jj][1]]) for jj in range(i, j))


def _card_spans(text, quiet=None, overlong=None):
    """[(start, end, kind)] for card candidates. A run of digit groups (a CSV row with an expiry, a list number
    before the card, two cards in a row) yields every card in it: the longest stretch of whole groups from each start
    that passes length, prefix and Luhn, overlapping stretches merged into one span. When the merged stretches end at
    different groups, which one is the card is unknown and the last four could be a neighbour's, so (start, end) goes
    into quiet and the tag shows no digits. A card whose stretch has a shorter card inside it from the same start
    goes into overlong: its tag shows no digits either, unless an SSN clips it down to that shorter card. Between and
    beside the cards, a card-grouped stretch that fails Luhn is hidden as long-number: when it fills its gap, or
    when every other group there is shorter than four digits. A run that yields any span, or holds a plain long
    number (a group of 10+ digits), also yields the SSN-shaped dash groups glued into it (_glued_ssns)."""
    out = []
    for m in _CARD.finditer(text):
        raw, base = m.group(0), m.start()
        groups = [(g.start(), g.end()) for g in _CARD_PIECE.finditer(raw)]
        if "." not in raw and is_card(re.sub(r"\D", "", raw)):
            out.append((m.start(), m.end(), "card"))
            if overlong is not None and _has_shorter_card(raw, groups, 0, len(groups) - 1):
                overlong.add(out[-1][:2])
            out.extend((base + a, base + z, k) for a, z, k in _glued_ssns(raw, groups))
            continue
        found = len(out)

        def card_ok(piece, i, j):
            return _card_stretch_ok(piece)

        long_before = [0]  # long_before[k]: groups of 4+ digits among groups[:k]
        for s, e in groups:
            long_before.append(long_before[-1] + (e - s >= 4))
        # The longest card stretch from every start, overlapping ones merged: a stretch that passes Luhn by chance
        # can overlap the real card, and keeping only one of them would leave the other's digits in the text.
        cards = []
        for i in range(len(groups)):
            b = _longest_stretch(raw, groups, (i,), len(groups), card_ok)
            if not b:
                continue
            longer = _has_shorter_card(raw, groups, b[0], b[1])
            if cards and b[0] <= cards[-1][1]:
                c0, c1, merged, over = cards[-1]
                cards[-1] = (c0, max(c1, b[1]), merged or b[1] != c1, over or longer)
            else:
                cards.append((b[0], b[1], False, longer))

        def misread_piece(a, z):  # a Luhn-failing card-grouped stretch with an issuer prefix
            piece = raw[groups[a][0]:groups[z][1]]
            n = len(re.sub(r"\D", "", piece))
            return (13 <= n <= 19 and bool(_CARD_GROUPED.match(piece)) and ("." not in piece or n >= 15)
                    and card_prefix_ok(re.sub(r"\D", "", piece)))

        gaps, prev = [], 0
        for ci, cj, ambiguous, longer in cards:
            out.append((base + groups[ci][0], base + groups[cj][1], "card"))
            if ambiguous and quiet is not None:
                quiet.add(out[-1][:2])
            if longer and overlong is not None:
                overlong.add(out[-1][:2])
            # A misread stretch overlapping the card's start or end: hide the part outside the card as well.
            head = min((a for a in range(max(prev, ci - 6), ci) for z in range(ci, min(cj, a + 6))
                        if misread_piece(a, z)), default=ci)
            tail = max((z for a in range(ci + 1, cj + 1) for z in range(cj + 1, min(len(groups), a + 6))
                        if misread_piece(a, z)), default=cj)
            if head < ci:
                out.append((base + groups[head][0], base + groups[ci - 1][1], "long-number"))
            if tail > cj:
                out.append((base + groups[cj + 1][0], base + groups[tail][1], "long-number"))
            gaps.append((prev, head))
            prev = tail + 1
        gaps.append((prev, len(groups)))
        for lo, hi in gaps:
            def misread_ok(piece, a, z, lo=lo, hi=hi):
                n = len(re.sub(r"\D", "", piece))
                if not _CARD_GROUPED.match(piece) or ("." in piece and n < 15):
                    return False
                outside = long_before[hi] - long_before[lo] - (long_before[z + 1] - long_before[a])
                return outside == 0
            b = _longest_stretch(raw, groups, range(lo, hi), hi, misread_ok)
            if b:
                out.append((base + groups[b[0]][0], base + groups[b[1]][1], "long-number"))
            else:
                out.extend((base + groups[a][0], base + groups[z][1], "long-number")
                           for a, z in _misread_tiling(raw, groups, lo, hi))
        if len(out) > found or any(e - s >= 10 for s, e in groups):
            out.extend((base + a, base + z, k) for a, z, k in _glued_ssns(raw, groups))
    return out


def _glued_ssns(raw, groups):
    """[(start, end, kind)] of 3-2-4 dash groups in a run that pass ssn_kind but sit glued to the run by a dash
    (a card, a dash, then ddd-dd-dddd), where _SSN_DASHED's edges don't let it match. Only asked for a run that holds
    a card or a hidden number: the SSN outranks the card, so find_spans clips the card around it, and without this
    the SSN's digits stayed in the text beside the card (or under its tag)."""
    out = []
    for i in range(len(groups) - 2):
        (a0, a1), (b0, b1), (c0, c1) = groups[i:i + 3]
        if (a1 - a0, b1 - b0, c1 - c0) != (3, 2, 4) or raw[a1] != "-" or raw[b1] != "-":
            continue
        if raw[a0 - 1:a0] != "-" and raw[c1:c1 + 1] != "-":
            continue  # not glued: _SSN_DASHED finds it on its own
        k = ssn_kind(raw[a0:a1], raw[b0:b1], raw[c0:c1])
        if k:
            out.append((a0, c1, k))
    return out


_EXPIRY_GROUP = re.compile(r"^(?:(?:0[1-9]|1[0-2])\d\d|20\d\d)$")  # MMYY, or a year 20xx


def _misread_tiling(raw, groups, lo, hi):
    """[(i, j)] group ranges to hide as long-number when Luhn-failing card-grouped stretches sit beside 4-digit
    groups. The gap must split into such stretches (each with a card issuer prefix, which keeps IDs like
    2030-0042-0017-0003-0001 clean) and other groups of at most four digits; an expiry-shaped one (MMYY, 20xx) is
    hidden with the stretch before it (or after it, at the start), any other is left in the text."""
    best = {lo: (0, None)}  # k -> (digits in stretches, (previous k, tile))
    for k in range(lo, hi):
        if k not in best:
            continue
        score = best[k][0]
        glen = groups[k][1] - groups[k][0]
        text_k = raw[groups[k][0]:groups[k][1]]
        moves = []
        if glen < 4:
            moves.append((k + 1, score, ("free", k, k)))
        elif glen == 4:  # an expiry is hidden with its stretch; any other 4-digit group is left as it is
            moves.append((k + 1, score, ("expiry" if _EXPIRY_GROUP.match(text_k) else "free", k, k)))
        n = 0
        for j in range(k, hi):
            n += groups[j][1] - groups[j][0]
            if n > 19:
                break
            piece = raw[groups[k][0]:groups[j][1]]
            digits = re.sub(r"\D", "", piece)
            if (n >= 13 and _CARD_GROUPED.match(piece) and ("." not in piece or n >= 15)
                    and card_prefix_ok(digits)):
                moves.append((j + 1, score + n, ("stretch", k, j)))
        for nk, sc, tile in moves:
            if nk not in best or sc > best[nk][0]:
                best[nk] = (sc, (k, tile))
    if hi not in best or best[hi][0] == 0:
        return []
    tiles, k = [], hi
    while k != lo:
        k, tile = best[k][1]
        tiles.append(tile)
    tiles.reverse()
    spans, pending = [], None
    for kind, a, z in tiles:
        if kind == "stretch":
            spans.append([pending if pending is not None else a, z])
            pending = None
        elif kind == "expiry":
            if spans and spans[-1][1] == a - 1:
                spans[-1][1] = a
            elif pending is None:
                pending = a
    return [tuple(sp) for sp in spans]


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
_EMAIL_LOCAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
_EMAIL_DOMAIN = re.compile(r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _is_word(ch):
    return ch.isalnum() or ch == "_"  # what \b treats as a word character in a str pattern


def _email_spans(text):
    """[(start, end)] of what _EMAIL.finditer matches, in linear time. The regex retries its local part from every
    start in a long run with no @ (quadratic: about a minute on 100k dash-joined digit groups); here each @ is found
    once, its local part is the run of local characters before it (back to the previous match), and the match starts
    at the leftmost word boundary in that run, as the regex's leftmost match would."""
    out, pos = [], 0
    at = text.find("@")
    while at != -1:
        m = _EMAIL_DOMAIN.match(text, at)
        if m:
            s = at
            while s > pos and text[s - 1] in _EMAIL_LOCAL:
                s -= 1
            k = next((k for k in range(s, at) if (k > 0 and _is_word(text[k - 1])) != _is_word(text[k])), None)
            if k is not None:
                out.append((k, m.end()))
                pos = m.end()
        at = text.find("@", max(at + 1, pos))
    return out
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


def raw_spans(text, quiet=None, overlong=None, glued=None):
    """Every candidate (start, end, kind) before overlap resolution. Sets passed as quiet and overlong collect the
    card spans whose tag must show no digits (see _card_spans); a list passed as glued collects the SSNs found
    glued into digit runs (_glued_ssns)."""
    out = []

    def add(m, kind, group=0):
        out.append((m.start(group), m.end(group), kind))

    for rx, kind in ((_PRIVATE_KEY, "private-key"), (_API_KEY, "api-key"), (_OTP, "2fa"),
                     (_PASSWORD, "password"), (_PIN, "pin"), (_SECRET, "secret"), (_MBI, "medicare"),
                     (_MRZ, "passport"), (_EIN, "ein")):
        for m in rx.finditer(text):
            add(m, kind)
    out.extend((s, e, "email") for s, e in _email_spans(text))
    for m in _SSN_DASHED.finditer(text):
        k = ssn_kind(*m.groups())
        if k:
            add(m, k)
    for m in _SSN_CTX.finditer(text):
        k = ssn_kind(*m.groups())
        if k:
            out.append((m.start(1), m.end(3), k))
    cards = _card_spans(text, quiet, overlong)
    out.extend(cards)
    if glued is not None:  # the only SSNs _card_spans yields are glued ones
        glued.extend(c for c in cards if c[2] in ("ssn", "itin"))
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


_PIECE = re.compile(r"[^\s.-](?:[\s\S]*[^\s.-])?")  # a leftover stretch without its edge separators


class _Disjoint:
    """Non-overlapping (start, end) spans, with the ones overlapping a query found by bisection.

    Since no two overlap, sorted by start their ends are sorted too, and the spans overlapping [s, e) are one
    contiguous run. Spans settled earlier sit in one pair of lists; new ones go into a second pair (insort, which
    mostly appends when they come in start order) until settle() merges the two. find_spans settles once per kind,
    so the work stays near-linear; checking every taken span per candidate was quadratic in the number of hits."""

    def __init__(self):
        self.old_s, self.old_e, self.new_s, self.new_e = [], [], [], []

    def add(self, s, e):
        i = bisect.bisect_left(self.new_s, s)
        while i < len(self.new_s) and self.new_s[i] == s and self.new_e[i] < e:
            i += 1  # an empty span sorts before a span with the same start
        self.new_s.insert(i, s)
        self.new_e.insert(i, e)

    def settle(self):
        if self.new_s:
            both = sorted(zip(self.old_s + self.new_s, self.old_e + self.new_e))
            self.old_s, self.old_e = [a for a, _ in both], [b for _, b in both]
            self.new_s, self.new_e = [], []

    def overlapping(self, s, e):
        """Sorted [(start, end)] of the spans with start < e and s < end."""
        return sorted((starts[i], ends[i]) for starts, ends in ((self.old_s, self.old_e), (self.new_s, self.new_e))
                      for i in range(bisect.bisect_right(ends, s), bisect.bisect_left(starts, e)))


def find_spans(text):
    """Non-overlapping spans [(start, end, kind, tag)], sorted by start.

    An SSN glued into a digit run (_glued_ssns) is hidden whole, and no tag shows any of its digits: a span it cuts
    into (an account number running into it, an e-mail whose local part holds it) keeps only its parts outside the
    SSN, each tagged without digits, and an SSN cut by a higher-priority span is hidden in pieces too."""
    quiet, overlong, glued = set(), set(), []
    raw = raw_spans(text, quiet, overlong, glued)
    rank = {k: i for i, k in enumerate(PRIORITY)}
    cands = sorted(raw, key=lambda s: (rank[s[2]], s[0], -(s[1] - s[0])))
    glued = set(glued)
    taken, spans, glued_taken = [], _Disjoint(), set()
    cur = None

    def take(ts, te, tk, tag):
        taken.append((ts, te, tk, tag))
        spans.add(ts, te)

    for s, e, k in cands:
        if rank[k] != cur:
            spans.settle()  # a new rank: this rank's spans join the settled ones
            cur = rank[k]
        hits = spans.overlapping(s, e)
        if not hits:
            take(s, e, k, "[CARD]" if k == "card" and ((s, e) in quiet or (s, e) in overlong) else _tag(k, text[s:e]))
            if (s, e, k) in glued:
                glued_taken.add((s, e))
            continue
        if k not in ("card", "long-number"):
            if (s, e, k) in glued or any(h in glued_taken for h in hits):
                # A glued SSN cut by a higher span, or a span cutting into a glued SSN: hide what lies outside the
                # spans already taken, tagged without digits, so neither the SSN's digits nor the span's own stay.
                tag = "[SECRET]" if k in HOLD_KINDS else TAGS.get(k, f"[{k.upper()}]")
                pos = s
                for ts, te in hits + [(e, e)]:
                    lo, hi = max(pos, s), min(ts, e)
                    m = _PIECE.search(text, lo, hi) if lo < hi else None
                    if m:
                        take(m.start(), m.end(), k, tag)
                    pos = max(pos, te)
            continue
        # A card or number partly inside a higher-priority span (618-91-5555.5555-5555.4444 read as an SSN first):
        # hide the digits left outside it too, or the rest of the card would stay in the text. A card is counted
        # once: its last piece is the card, earlier pieces are numbers. The tag shows the card's last four only when
        # that piece is a whole card itself (the card beside a glued SSN), or holds all four and ends where the card
        # ends; otherwise it is [CARD], so no digit hidden under the other span leaks through the tag. An ambiguous
        # (quiet) card always gets [CARD]; an overlong one shows a last four only when clipped to a whole card.
        pieces, pos = [], s
        for ts, te in hits + [(e, e)]:
            lo, hi = max(pos, s), min(ts, e)
            m = re.search(r"\d(?:.*\d)?", text[lo:hi]) if lo < hi else None
            if m:
                pieces.append((lo + m.start(), lo + m.end()))
            pos = max(pos, te)
        for i, (ps, pe) in enumerate(pieces):
            if k == "card" and i == len(pieces) - 1:
                digits = re.sub(r"\D", "", text[ps:pe])
                if (s, e) in quiet:
                    show = False
                elif (s, e) in overlong:  # clipped to a whole card (the extra groups were an SSN's): its own four
                    show = is_card(digits)
                else:
                    show = is_card(digits) or (len(digits) >= 4 and not text[pe:e].strip(" .-"))
                take(ps, pe, "card", _tag("card", text[ps:pe]) if show else "[CARD]")
            else:
                take(ps, pe, "long-number", TAGS["long-number"])
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
        parts, pos = [], 0
        for s, e, _, tag in spans:  # one join: splicing each span into the string was quadratic in span count
            parts += [text[pos:s], tag]
            pos = e
        text = "".join(parts) + text[pos:]
    return text, dict(total)


def fmt_counts(c):
    return ", ".join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda kv: PRIORITY.index(kv[0])
                                                    if kv[0] in PRIORITY else 99)) or "none"
