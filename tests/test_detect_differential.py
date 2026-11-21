"""The detector hides at least what the round-5 detector (tests/_detect_r5.py, frozen at 0ffa68e) hid.

Over a seeded corpus of fake texts (cards, SSNs and ITINs labelled or glued into digit runs, long numbers, dates,
account, license and SSN labels, every joiner), every digit the frozen copy hid, through find_spans alone or through
redact's re-scans, is hidden by doc_intake.detect too, and no last-four tag shows a digit of a valid SSN shape. All
values are fake: test card numbers, and SSN shapes built at run time.
"""
import random
import re

import _detect_r5 as frozen
from doc_intake import detect


def _ssn(area, group, serial):
    return area + "-" + group + "-" + serial


SSNS = [_ssn("273", "81", "6495"), _ssn("078", "05", "1120"), _ssn("123", "45", "6789")]
ITIN = _ssn("912", "70", "1234")
PIECES = SSNS + [ITIN, _ssn("666", "45", "6789"), "4111-1111-1111-1111", "4111111111111111", "5555 5555 5555 4444",
                 "4111-1111-1111-1112", "4111 1111 1111 1111 912", "12345678901", "9999999999", "1234567890",
                 "123456789", "12345", "12345678", "30722", "2030", "12/29", "01/02/1990", "12.50", "3.00", "912",
                 "12-3456789", "212-555-0142", "111111118", "someone@example.com", "@example.com", "1234 5678 9012"]
LABELS = ["Account: ", "Acct # ", "SSN: ", "SSN ", "TIN: ", "Social Security Number: ", "Driver license ", "DL ",
          "DOB: ", "EIN ", "Phone ", "Card ", "Ref ", "Member ID ", "someone", ""]
JOINS = ["-", "-", ".", " ", "/", "--", "- ", "", "\n"]


def _corpus(n, seed=20301121):
    rng = random.Random(seed)
    for _ in range(n):
        parts = []
        for _ in range(rng.randint(1, 5)):
            if rng.random() < 0.35:
                parts.append(rng.choice(LABELS))
            r = rng.random()
            if r < 0.3:  # a run with an SSN glued into it, beside a card or a long number, with or without a tail
                ssn = rng.choice(SSNS + [ITIN])
                run = rng.choice(["4111-1111-1111-1111", "4111 1111 1111 1111", "12345678901", "9999999999",
                                  "4111-1111-1111-1112", "5555-5555-5555-4444"])
                tail = rng.choice(["", "-12", "-30722", "--12", " -12", "-4111-1111", "/1234", "-12.50", "-01/02/1990"])
                parts.append((run + "-" + ssn if rng.random() < 0.6 else ssn + "-" + run) + tail)
            elif r < 0.85:
                parts.append(rng.choice(PIECES))
            else:
                parts.append("".join(rng.choice("0123456789") for _ in range(rng.randint(1, 11))))
            parts.append(rng.choice(JOINS))
        yield "".join(parts) + "end"


def _covered(spans, n):
    c = [False] * n
    for s, e, _, _ in spans:
        for k in range(s, e):
            c[k] = True
    return c


def _frozen_redact_kept(text):
    """Positions in text whose characters the frozen redact left as they were (its passes, tracked)."""
    chars, orig = list(text), list(range(len(text)))
    for _ in range(3):
        spans = frozen.find_spans("".join(chars))
        if not spans:
            break
        nc, no, pos = [], [], 0
        for s, e, _, tag in spans:
            nc += chars[pos:s] + list(tag)
            no += orig[pos:s] + [None] * len(tag)
            pos = e
        chars, orig = nc + chars[pos:], no + orig[pos:]
    return {o for o in orig if o is not None}


def _ssn_shapes(text):
    """Positions of the digits of every valid ddd-dd-dddd shape in text."""
    out = set()
    for m in re.finditer(r"(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)", text):
        if detect.ssn_kind(*m.groups()):
            out.update(range(m.start(), m.end()))
    return out


def _tag_free_digits(red):
    return re.sub(r"\D", "", re.sub(r"\[[A-Z0-9-]+(?::\d{4})?\]", "", red))


def _is_subsequence(small, big):
    it = iter(big)
    return all(ch in it for ch in small)


def test_hides_at_least_what_round_five_hid():
    texts = list(_corpus(3000))
    for text in texts:
        digits = [k for k, ch in enumerate(text) if ch.isdigit()]
        spans = detect.find_spans(text)
        new_cov = _covered(spans, len(text))
        old_cov = _covered(frozen.find_spans(text), len(text))
        assert all(new_cov[k] for k in digits if old_cov[k]), ("find_spans", text)

        red, _, kept = detect._redact_map(text)
        kept = set(kept)
        old_kept = _frozen_redact_kept(text)
        assert all(k in old_kept for k in digits if k in kept), ("redact", text)
        # and, without trusting the tracking: the digits left showing are a subsequence of those the frozen copy left
        old_red, _ = frozen.redact(text)
        assert _is_subsequence(_tag_free_digits(red), _tag_free_digits(old_red)), ("redact text", text)

        # no last-four tag in find_spans takes a digit from a valid SSN shape
        shapes = _ssn_shapes(text)
        for s, e, _, tag in spans:
            if "last4:" in tag:
                last4 = [k for k in range(s, e) if text[k].isdigit()][-4:]
                assert not shapes.intersection(last4), ("tag", text, tag)
        # nor in redact: a serial that appears in the text only inside SSN shapes never shows in a tag
        for serial in re.findall(r"last4:(\d{4})", red):
            inside = sum(1 for m in re.finditer(r"(?<!\d)\d{3}-\d{2}-(\d{4})(?!\d)", text)
                         if m.group(1) == serial and detect.ssn_kind(*m.group(0).split("-")))
            if inside and text.count(serial) == inside:
                raise AssertionError(("redact tag", text, red))


def test_corpus_reaches_the_shapes():
    # the corpus does hold glued SSNs, labelled SSNs, cards and long numbers, so the test above isn't vacuous
    texts = list(_corpus(3000))
    assert sum(any(s in t for s in SSNS) for t in texts) > 500
    assert sum(bool(detect._context(t)[1]) for t in texts) > 1000
    assert sum("4111" in t for t in texts) > 500


def _s():
    return _ssn("273", "81", "6495")


REPORTED = [  # shapes a review found showing digits round 5 hid
    lambda: "SSN: " + _s() + "-" + _s() + "/1234",
    lambda: "SSN: 307224111-1111-1111-1112-0001/" + _s(),
    lambda: "TIN: 273816495-" + ITIN + "/1234-5678",
    lambda: "Social Security Number: " + ITIN + "-" + _s() + "/12345678901",
    lambda: "Driver license 12345678901-" + _s() + "--12",
    lambda: "Driver license 12345678901-" + _s() + " -12",
    lambda: "DL " + _s() + "-4111-1111-1111-1112-0001--99 12345",
    lambda: "SSN: " + _s() + "-12.50/3.00",
    lambda: "Driver license " + _s() + "-4111-1111 4111111111111111",
    lambda: "Driver license 9999999999." + _s() + "-1234 5678 9012",
    lambda: "Account: 12345678901-" + _s() + " 4111111111111111",
    lambda: "Acct # 12345-" + _s() + " 4111111111111111",
]


def test_reported_shapes_hide_at_least_what_round_five_hid():
    for make in REPORTED:
        text = make()
        digits = [k for k, ch in enumerate(text) if ch.isdigit()]
        new_cov = _covered(detect.find_spans(text), len(text))
        old_cov = _covered(frozen.find_spans(text), len(text))
        assert all(new_cov[k] for k in digits if old_cov[k]), text
        kept, old_kept = set(detect._redact_map(text)[2]), _frozen_redact_kept(text)
        assert all(k in old_kept for k in digits if k in kept), text
    # a decimal after the SSN is left whole, not cut into a hidden number and a visible .50
    red, _ = detect.redact("SSN: " + _s() + "-12.50/3.00")
    assert red == "SSN: [SSN]-12.50/3.00", red
