"""The detectors (detect.py), scan and the held folder, duplicates against the records and within a batch, the
records fingerprint cache, file sniffing, a missing tesseract, and a real PDF through pdftotext, all through the
fake host (tests/host.py) against a throwaway records root.

Every value here is fake (see tests/helpers.py).
"""
import json
import os
import random
import re
import shutil

import pytest

import host
from doc_intake import detect, fingerprints, places, reader, tasks
from doc_intake.config import CFG
from doc_intake.places import DocError
from helpers import ACCOUNT, CARD, CARD_SPACED, EIN, FAKES, MBI, ROUTING, SSN, drop, inbox, mode, run

INBOX = host.INBOX
STATE = host.STATE



def test_approve_refuses_without_a_terminal(rec, capsys):
    # the assistant's shell is not a terminal: approve must refuse there, so it can never approve its own reads
    rc = host.main(["approve", "no-such-task"], ask_fn=lambda _p: "")
    assert rc != 0
    assert "user's own terminal" in capsys.readouterr().err


# ---------------------------------------------------------------- detectors
@pytest.mark.parametrize("text,kind", [
    (f"SSN {SSN}", "ssn"),
    ("social security number 078051120", "ssn"),
    (f"card {CARD}", "card"),
    (f"card {CARD_SPACED}", "card"),
    ("amex 378282246310005", "card"),
    ("card 4111.1111.1111.1111", "card"),
    ("card 5555.5555.5555.4444", "card"),
    ("amex 3782.822463.10005", "card"),
    ("Card No.6011.1111.1111.1117.", "card"),
    (f"routing {ROUTING}", "routing"),
    (f"Account number: {ACCOUNT}", "account"),
    ("Date of birth: 01/02/1980", "dob"),
    ("DOB Jan 2, 1980", "dob"),
    (f"Medicare number {MBI}", "medicare"),
    ("Passport No. C12345678", "passport"),
    ("P<USAEXAMPLE<<SAMPLE<<<<<<<<<<<<<<<<<<<<<<<<", "passport"),
    ("Driver's license A12-34-5678", "drivers-license"),
    ("Member ID: XYZ123456789", "member-id"),
    ("Group number 0012345", "member-id"),
    ("Employee ID 00123456", "employee-id"),
    (f"EIN {EIN}", "ein"),
    ("mail someone@example.com", "email"),
    ("Password: hunter22", "password"),
    ("Your PIN is 4821", "pin"),
    ("api_key = abcdefgh12345678", "secret"),
    ("AKIA" + "ABCDEFGHIJKLMNOP", "api-key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", "private-key"),
    ("otpauth://totp/Example:me?secret=JBSWY3DPEHPK3PXP", "2fa"),
    ("Save these backup codes", "2fa"),
])
def test_each_detector(text, kind):
    assert kind in detect.counts(detect.find_spans(text))


def test_luhn_rejects_random_digits():
    rng = random.Random(7)
    checked = 0
    for _ in range(500):
        d = "4" + "".join(rng.choice("0123456789") for _ in range(15))
        if detect.luhn_ok(d):
            continue
        checked += 1
        assert "card" not in detect.counts(detect.find_spans(f"card {d}"))
    assert checked > 400
    assert not detect.is_card("4111111111111112")
    assert detect.is_card(CARD)


def test_aba_rejects_random_digits():
    rng = random.Random(11)
    checked = 0
    for _ in range(500):
        d = "".join(rng.choice("0123456789") for _ in range(9))
        if detect.aba_ok(d):
            continue
        checked += 1
        assert "routing" not in detect.counts(detect.find_spans(f"number {d}"))
    assert checked > 400
    assert not detect.aba_ok("123456789") and detect.aba_ok(ROUTING)


def test_dotted_card_needs_luhn_and_card_groups():
    assert detect.counts(detect.find_spans("card 4111.1111.1111.1112")) == {"long-number": 1}  # fails Luhn
    for t in ("1.234.567", "1,234.56", "$4,111.11", "v1.2.3.4", "192.168.1.1", "2030.01.05", "Total 4111.11",
              "4111.1111", "1234.5678.90"):
        assert detect.find_spans(t) == [], t
    assert "card" not in detect.counts(detect.find_spans("pi 3.14159265358979"))
    assert detect.find_spans("build 2024.1234.5678.9") == []  # dotted 13 digits failing Luhn: too short to hide


def _card_only(text):
    red, c = detect.redact(text)
    return c, re.sub(r"\[CARD-last4:\d{4}\]", "", red)


@pytest.mark.parametrize("text", [
    "Visa,4111.1111.1111.1111,12/30",  # a CSV row with the expiry after it
    "card 4111.1111.1111.1111.12/30",
    "1,4111.1111.1111.1111",
    "4111.1111 1111 1111", "4111-1111.1111-1111", "4111 1111.1111 1111",  # mixed separators
    "card 4111 1111 1111 1111 12/30",
    "4000.0000.0000.0000.006",  # 19-digit Visa
    "Diners 3600.000000.0008",  # 4-6-4
    "4222.2222.2222.2",  # 13-digit Visa, 4-4-4-1
    "1.4111.1111.1111.1111",  # a list number glued on: the card is still cut out
    "4111.1111.1111.1111.5",  # a trailing digit group is cut off the card
])
def test_card_in_a_run_of_groups_is_cut_out_and_redacted(text):
    c, rest = _card_only(text)
    assert c == {"card": 1}, text
    assert not re.search(r"\d{3}", rest), text  # nothing but a stray list or expiry digit or two is left


@pytest.mark.parametrize("text", ["4000.0000.0000.0000.007", "3782.822463.10006", "Order 2030.1111.2222.3333"])
def test_grouped_digits_failing_luhn_are_a_number_not_a_card(text):
    assert detect.counts(detect.find_spans(text)) == {"long-number": 1}


@pytest.mark.parametrize("text", ["4111 1111 1111 1111 5555 5555 5555 4444",
                                  "4111.1111.1111.1111.5555.5555.5555.4444",
                                  "4111-1111-1111-1111,5555-5555-5555-4444"])
def test_two_cards_in_one_run_are_both_found(text):
    assert detect.counts(detect.find_spans(text)) == {"card": 2}  # snapshots black out find_spans once
    red, _ = detect.redact(text)
    assert re.fullmatch(r"\[CARD-last4:1111\][ .,-]\[CARD-last4:4444\]", red), red


@pytest.mark.parametrize("text", ["ID-4111 1111 1111 1111", "Visa-4111 1111 1111 1111", "4111 1111 1111 1111-VISA",
                                  "4111-1111-1111-1111-"])
def test_card_touching_a_hyphen(text):
    c, rest = _card_only(text)
    assert detect.counts(detect.find_spans(text)) == {"card": 1} and c == {"card": 1}, text
    assert not re.search(r"\d{3}", rest), text


@pytest.mark.parametrize("text,want", [("2030-0042-0017-0003-0001", {}), ("Invoice INV-2030-0042", {}),
                                       ("123-45-6789", {"ssn": 1}), ("800-555-0142", {"phone": 1}),
                                       ("2030-01-05", {}), ("1.234.567", {}), ("192.168.1.1", {})])
def test_ordinary_hyphenated_numbers_stay_clean(text, want):
    assert detect.counts(detect.find_spans(text)) == want


@pytest.mark.parametrize("text", ["Visa 4111 1111 1111 1112 12 30", "Exp 12 30 4111 1111 1111 1112",
                                  "12 4111 1111 1111 1112", "1 4111-1111-1111-1112", "4111.1111.1111.1112 123"])
def test_misread_card_beside_other_groups_is_a_number(text):
    assert detect.counts(detect.find_spans(text)) == {"long-number": 1}
    red, _ = detect.redact(text)
    assert not re.search(r"\d{4}", red), text


@pytest.mark.parametrize("text,card,want", [
    ("2315 2594 2487 3782 822463 10005 9835", "3782 822463 10005", "[CARD] 9835"),  # 9835 isn't the card's
    ("2558.4012.8888.8888.1881", "4012.8888.8888.1881", "[CARD]")])
def test_valid_stretch_overlapping_a_card_leaves_nothing(text, card, want):
    # 2315259424873782 and 2558401288888888 pass is_card by chance and overlap the real test card. The merged span's
    # tag shows no digits: which stretch is the card is unknown, so its last four could be a neighbour's.
    s0 = text.index(card)
    spans = detect.find_spans(text)
    assert all(any(s <= k < e for s, e, _, _ in spans) for k in range(s0, s0 + len(card)) if text[k].isdigit())
    red, c = detect.redact(text)
    assert c == {"card": 1} and red == want


PUBLISHED_CARDS = [(CARD, (4, 4, 4, 4)), ("5555555555554444", (4, 4, 4, 4)), ("378282246310005", (4, 6, 5)),
                   ("6011111111111117", (4, 4, 4, 4)), ("4012888888881881", (4, 4, 4, 4))]


def _random_groups(rng, k):
    return ["".join(rng.choice("0123456789") for _ in range(rng.randint(1, 6))) for _ in range(k)]


def _join(rng, parts):
    """parts joined with a separator picked per join from ' .-'; returns (text, [(start, end) of each part])."""
    text, where = "", []
    for i, part in enumerate(parts):
        if i:
            text += rng.choice(" .-")
        where.append((len(text), len(text) + len(part)))
        text += part
    return text, where


def _card_groups(card, shape):
    pos, out = 0, []
    for n in shape:
        out.append(card[pos:pos + n])
        pos += n
    return out


def _covered(spans, offset, where):
    return all(any(s <= offset + k < e for s, e, _, _ in spans) for a, z in where for k in range(a, z))


def test_card_among_random_digit_groups_is_always_covered():
    rng = random.Random(20301105)
    for _ in range(2500):
        groups = _card_groups(*rng.choice(PUBLISHED_CARDS))
        before, after = _random_groups(rng, rng.randint(0, 4)), _random_groups(rng, rng.randint(0, 3))
        if rng.random() < 0.5:
            sep = rng.choice(" .-")  # one separator throughout
            body, where = sep.join(before + groups + after), []
            pos = len(sep.join(before)) + (len(sep) if before else 0)
            for g in groups:
                where.append((pos, pos + len(g)))
                pos += len(g) + 1
        else:  # a separator picked per join
            body, all_where = _join(rng, before + groups + after)
            where = all_where[len(before):len(before) + len(groups)]
        text = "Ref " + body + " end"
        assert _covered(detect.find_spans(text), 4, where), text


def test_two_cards_among_random_digit_groups_are_always_covered():
    rng = random.Random(20301106)
    for _ in range(1500):
        first, second = _card_groups(*rng.choice(PUBLISHED_CARDS)), _card_groups(*rng.choice(PUBLISHED_CARDS))
        between = _random_groups(rng, rng.randint(0, 3))
        body, where = _join(rng, first + between + second)
        where = where[:len(first)] + where[len(first) + len(between):]
        text = "Ref " + body + " end"
        assert _covered(detect.find_spans(text), 4, where), text


def test_twenty_digit_spaced_run_is_pinned():
    # 4000 0000 0000 0000 0006: no 13-19 digit stretch passes Luhn; 4000 0000 0000 0000 is a misread with an issuer
    # prefix, so it is hidden and the extra 0006 group is left.
    red, c = detect.redact("4000 0000 0000 0000 0006")
    assert c == {"long-number": 1} and red == "[NUMBER] 0006"


def test_misread_beside_two_arbitrary_groups():
    text = "Card 4111 1111 1111 1112 1234 5678"
    assert detect.counts(detect.find_spans(text)) == {"long-number": 1}
    red, _ = detect.redact(text)
    assert "4111" not in red and "1112" not in red and red == "Card [NUMBER] 5678"  # 1234 reads as MMYY, hidden too


def test_merged_card_span_tag():
    # 5555555555554444 and 5555555544443972 are both cards and merge; 5555 4444 3972 9815 has a card prefix but fails
    # Luhn, so its part past the card (9815) is hidden as a number. The stretches end at different groups, so the tag
    # shows no digits: 3972, the merged span's last four, is the published card's neighbour, not its own.
    text = "5555-5555-5555-4444-3972-9815"
    c = detect.counts(detect.find_spans(text))
    assert c == {"card": 1, "long-number": 1}
    red, _ = detect.redact(text)
    assert red == "[CARD]-[NUMBER]"
    # stretches that end together keep the last four: they agree on it
    red, c = detect.redact("4111 1111 1111 1111 5555 5555 5555 4444")
    assert c == {"card": 2} and red == "[CARD-last4:1111] [CARD-last4:4444]"


def _ssn_like(area, group, serial):
    return area + "-" + group + "-" + serial  # built at run time, as helpers.SSN is


@pytest.mark.parametrize("text,want_red,want_counts", [
    ("Card 4111-1111-1111-1111-{ssn}", "Card [CARD-last4:1111]-[SSN]", {"card": 1, "ssn": 1}),
    ("Card {ssn}-4111-1111-1111-1111 end", "Card [SSN]-[CARD-last4:1111] end", {"ssn": 1, "card": 1}),
    ("Card 4111 1111 1111 1111-{ssn}", "Card [CARD-last4:1111]-[SSN]", {"card": 1, "ssn": 1}),
    ("Card 5555-5555-5555-4444-{ssn}-12", "Card [CARD-last4:4444]-[SSN]-12", {"card": 1, "ssn": 1}),
    ("Card 4111-1111-1111-1112-{ssn}", "Card [NUMBER]-[SSN]", {"long-number": 1, "ssn": 1}),  # a misread too
    ("Card 4111-1111-1111-1111-{itin}", "Card [CARD-last4:1111]-[ITIN]", {"card": 1, "itin": 1}),
])
def test_ssn_glued_to_a_card_is_hidden(text, want_red, want_counts):
    # 4111111111111111078 passes is_card by chance, so the card swallowed 078 and its tag showed 1078, while 05-1120
    # stayed in the text; with another SSN the whole group stayed. The SSN-shaped group is now its own span.
    text = text.format(ssn=SSN, itin=_ssn_like("912", "70", "1234"))
    red, c = detect.redact(text)
    assert red == want_red and c == want_counts, red
    assert detect.counts(detect.find_spans(text)) == want_counts  # one find_spans pass counts it the same


def test_ssn_glued_to_a_card_never_leaves_a_digit():
    rng = random.Random(780511)
    for _ in range(400):
        ssn = _ssn_like(f"{rng.randint(1, 665):03d}", f"{rng.randint(1, 99):02d}", f"{rng.randint(1, 9999):04d}")
        if detect.ssn_kind(*ssn.split("-")) is None:
            continue
        card = rng.choice(["4111-1111-1111-1111", "5555-5555-5555-4444", "4012-8888-8888-1881", "3782-822463-10005"])
        text = "Ref " + (card + "-" + ssn if rng.random() < 0.5 else ssn + "-" + card) + " end"
        red, c = detect.redact(text)
        assert c == {"card": 1, "ssn": 1}, text
        assert not re.search(r"\d", re.sub(r"\[CARD-last4:\d{4}\]", "", red)), text
        tag = re.search(r"\[CARD-last4:(\d{4})\]", red)
        assert tag and tag.group(1) == card[-4:], text  # the card's own last four, never the SSN's digits


@pytest.mark.parametrize("text", ["Ref 2030-{ssn}-0001", "Part 12-{ssn}", "{ssn}-2030"])
def test_ssn_shaped_group_in_an_id_without_a_card_is_unchanged(text):
    # only a run holding a card or a hidden number gives up its glued SSN-shaped groups; plain IDs stay as before
    assert detect.find_spans(text.format(ssn=SSN)) == []


def test_receipt_lines_stay_clean():
    assert detect.find_spans("Store 0042 Reg 03 Trans 0017 Time 12:34 Auth 4111") == []
    red, c = detect.redact("Total 4111 1111 1111 1111 $12.50")
    assert c == {"card": 1} and red == "Total [CARD-last4:1111] $12.50"


def test_card_partly_inside_a_higher_priority_span_is_still_hidden():
    # 618-91-5555 reads as an SSN first (found by the fuzz test); the card digits outside it are hidden as well
    red, c = detect.redact("Ref 384.932.618-91-5555.5555-5555.4444.062 end")
    assert c == {"ssn": 1, "card": 1} and red == "Ref 384.932.[SSN].[CARD-last4:4444].062 end"
    # the SSN takes the end of a 19-digit card: the head piece is the card's last piece, so it is the card (counted
    # once), but it doesn't end where the card ends, so its tag shows no digits
    red, c = detect.redact("Ref 4000 0000 0000 0000 006-12-3456 end")
    assert c == {"card": 1, "ssn": 1} and red == "Ref [CARD] [SSN] end"


def _luhn_complete(partial):
    return next(partial + d for d in "0123456789" if detect.luhn_ok(partial + d))


def test_card_cut_in_the_middle_counts_once():
    rng = random.Random(4019)
    for _ in range(50):
        ssn = rng.choice(["234", "345", "456"]) + "-" + rng.choice(["12", "34", "56"]) + "-" + str(rng.randint(1000, 9999))
        head = "4" + "".join(rng.choice("0123456789") for _ in range(3))
        tail = "".join(rng.choice("0123456789") for _ in range(5))
        card = _luhn_complete(head + ssn.replace("-", "") + tail)  # a 19-digit Visa
        assert detect.is_card(card)
        text = f"Ref {card[:4]} {ssn} {card[13:16]} {card[16:]} end"  # dddd ddd-dd-dddd ddd ddd
        spans = detect.find_spans(text)
        assert detect.counts(spans) == {"card": 1, "ssn": 1, "long-number": 1}, text
        digits = [k for k, ch in enumerate(text) if ch.isdigit()]
        assert all(any(s <= k < e for s, e, _, _ in spans) for k in digits), text
        tags = [tag for _, _, kind, tag in spans if kind in ("card", "long-number")]
        assert tags == ["[NUMBER]", f"[CARD-last4:{card[-4:]}]"], text  # last four only on the final piece
    # a final piece under four digits shows no digits at all, so none hidden under the SSN leak through the tag
    card = _luhn_complete("4123" + "234" + "56" + "7890" + "12")  # a 16-digit Visa
    red, c = detect.redact(f"Ref {card[:4]} {card[4:7]}-{card[7:9]}-{card[9:13]} {card[13:]} end")
    assert c == {"card": 1, "ssn": 1, "long-number": 1} and red == "Ref [NUMBER] [SSN] [CARD] end"


def test_clip_leaves_no_digit_two_cards():
    glued = "618" + "-91-" + "5555"
    cards = ["5555-5555-5555-4444", "4012-8888-8888-1881"]
    text = "Ref " + ".".join([glued, cards[0], glued, cards[1], glued]) + " end"
    spans = detect.find_spans(text)
    digits = [k for k, ch in enumerate(text) if ch.isdigit()]
    assert all(any(s <= k < e for s, e, _, _ in spans) for k in digits), detect.redact(text)[0]
    red, _ = detect.redact(text)
    assert not any(ch.isdigit() for ch in re.sub(r"\[CARD-last4:\d{4}\]", "", red)), red


@pytest.mark.parametrize("text", ["2030 0042 0017 0003 0001", "1234-5678-9012-3456-7890"])
def test_more_five_group_ids_stay_clean(text):
    assert detect.find_spans(text) == []


def test_ssn_rules():
    assert detect.ssn_kind("000", "12", "3456") is None
    assert detect.ssn_kind("666", "12", "3456") is None
    assert detect.ssn_kind("912", "70", "1234") == "itin"
    assert detect.ssn_kind("912", "10", "1234") is None


def test_plain_text_is_left_alone():
    t = "Total $1,234.56 on 2030-01-05. Deductible $500. Call before 5 pm."
    assert detect.find_spans(t) == []


def test_redaction_leaves_no_fake_value():
    text = "\n".join([f"SSN {SSN}", f"Card {CARD_SPACED}", f"Routing {ROUTING} Account {ACCOUNT}",
                      "DOB: 01/02/1980", f"Medicare {MBI}", "Passport No. C12345678", "Member ID: XYZ123456789",
                      "Employee ID 00123456", f"EIN {EIN}", "someone@example.com",
                      "Visa 5555.5555.5555.4444", "Amex 3782.822463.10005",
                      "Visa,6011.1111.1111.1117,12/30", "Mixed 5555 5555.5555-4444",
                      "Cards 4111 1111 1111 1111 5555 5555 5555 4444", "ID-3782 822463 10005"])
    red, c = detect.redact(text)
    for v in FAKES[:-1] + ["5555.5555", "822463", "10005", "6011", "5555 5555"]:
        assert v not in red
    assert "[CARD-last4:1111]" in red and "[ACCOUNT-last4:8901]" in red and "[SSN]" in red
    assert "Visa [CARD-last4:4444]\n" in red and "Amex [CARD-last4:0005]\n" in red
    assert "Visa,[CARD-last4:1117],12/30" in red and "Mixed [CARD-last4:4444]" in red
    assert "Cards [CARD-last4:1111] [CARD-last4:4444]" in red and red.endswith("ID-[CARD-last4:0005]")
    assert c["ssn"] == 1 and c["card"] == 8


# ---------------------------------------------------------------- scan and held
def test_scan_holds_credentials(rec, capsys):
    drop(rec, "logins.txt", "Example site\nPassword: hunter22\n")
    assert run(["scan"]) == 0
    out = capsys.readouterr().out
    assert "HELD logins.txt" in out and CFG.held_note in out
    assert "hunter22" not in out
    held = rec / INBOX / ".held" / "logins.txt"
    assert held.exists() and mode(held) == 0o600 and mode(held.parent) == 0o700
    idx = json.loads((rec / STATE / "index.json").read_text())
    assert [v["status"] for v in idx["files"].values()] == ["held"]
    assert mode(rec / STATE / "index.json") == 0o600
    with pytest.raises(DocError):
        tasks.resolve_record(str(held))
    assert run(["extract", str(held), "--pages", "1"]) == 2


def test_scan_holds_2fa_qr(rec, monkeypatch, capsys):
    monkeypatch.setattr(reader, "_zxing", object)  # any non-None module stand-in
    monkeypatch.setattr(reader, "read_barcodes", lambda imgs: [("QRCode", "otpauth://totp/x?secret=ABC")])
    monkeypatch.setattr(reader, "ocr", lambda img, notes: "scan this code")
    p = inbox(rec) / "qr.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    run(["scan"])
    assert "HELD qr.png" in capsys.readouterr().out
    assert (rec / INBOX / ".held" / "qr.png").exists()


def test_scan_table_counts_only(rec, capsys):
    # the table shows counts of what was found, never the values; the destination comes from the host's routing
    drop(rec, "stub.txt", f"Earnings Statement\nPay date: 01/15/2030\nNet pay 1,000.00\nSSN {SSN}\n")
    run(["scan"])
    out = capsys.readouterr().out
    assert "ssn 1" in out and SSN not in out
    assert "Pay/2030" in out and "2030-01-15 - Pay stub.txt" in out


def test_inbox_created_0700(rec):
    run(["scan"])
    assert mode(rec / INBOX) == 0o700


# ---------------------------------------------------------------- duplicates of existing records and within a batch
AGREEMENT = "Purchase agreement for an example house, dated 03/01/2030\n"


def existing(root, relp, text):
    p = root / relp
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_scan_matches_a_file_filed_before_intake(rec, capsys):
    existing(rec, "Housing/old/agreement.txt", AGREEMENT)
    drop(rec, "agreement copy.txt", AGREEMENT)
    assert run(["scan"]) == 0
    out = capsys.readouterr().out
    assert "fingerprinting existing records (first run)" in out
    assert "duplicate of Housing/old/agreement.txt" in out
    assert "Remove them after checking, once the backup is verified" in out
    cache = rec / STATE / "records-hashes.json"
    assert mode(cache) == 0o600
    entry = json.loads(cache.read_text())["files"]["Housing/old/agreement.txt"]
    assert set(entry) == {"size", "mtime_ns", "sha256", "text_fp"}
    assert (rec / INBOX / "agreement copy.txt").exists()


def test_duplicates_within_a_batch(rec, capsys):
    drop(rec, "a.txt", AGREEMENT)
    drop(rec, "b.txt", AGREEMENT)
    drop(rec, "c.txt", "a different letter\n")
    run(["scan"])
    out = capsys.readouterr().out
    assert f"duplicate of {INBOX}/a.txt" in out and "1 exact copy" in out
    assert "duplicate of" not in out.split("c.txt", 1)[1].splitlines()[0]
    assert (rec / INBOX / "a.txt").exists() and (rec / INBOX / "b.txt").exists()


def test_cache_does_not_rehash_unchanged_files(rec, monkeypatch):
    for i in range(3):
        existing(rec, f"Tax/2030/form {i}.txt", f"form {i}\n")
    calls = []
    real = places.sha256
    monkeypatch.setattr(places, "sha256", lambda p: calls.append(places.rel(p)) or real(p))
    fingerprints.records_hashes()
    assert len(calls) == 3
    calls.clear()
    hashes = fingerprints.records_hashes(out=lambda s: calls.append("msg: " + s))
    assert hashes
    assert calls == []
    changed = existing(rec, "Tax/2030/form 1.txt", "form 1, corrected\n")
    os.utime(changed, ns=(1, 1))
    existing(rec, "Tax/2030/form 9.txt", "form 9\n")
    (rec / "Tax" / "2030" / "form 0.txt").unlink()
    known = fingerprints.records_hashes()
    assert sorted(calls) == ["Tax/2030/form 1.txt", "Tax/2030/form 9.txt"]
    assert "Tax/2030/form 0.txt" not in known.values()
    cache = json.loads((rec / STATE / "records-hashes.json").read_text())["files"]
    assert "Tax/2030/form 0.txt" not in cache and "Tax/2030/form 9.txt" in cache


def test_skipped_folders_are_not_hashed(rec, monkeypatch):
    # the inbox (and its dot folders), the state folder, dotfiles and dot folders are never fingerprinted
    drop(rec, "new.txt", "inbox file\n")
    for relp in (f"{INBOX}/.held/secret.txt", f"{INBOX}/.done/receipt.txt", f"{STATE}/tasks/t.json",
                 ".trackerignore", "Housing/Example/.imported.json", ".hidden/inside.txt"):
        existing(rec, relp, "x\n")
    existing(rec, "Housing/Example/notes.txt", "kept\n")
    calls = []
    real = places.sha256
    monkeypatch.setattr(places, "sha256", lambda p: calls.append(places.rel(p)) or real(p))
    known = fingerprints.records_hashes()
    assert calls == ["Housing/Example/notes.txt"]
    assert list(known.values()) == ["Housing/Example/notes.txt"]


def test_missing_tesseract_is_graceful(rec, monkeypatch, capsys):
    real = reader.have
    monkeypatch.setattr(reader, "have", lambda t: False if t == "tesseract" else real(t))
    p = inbox(rec) / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)
    assert run(["scan"]) == 0
    out = capsys.readouterr().out
    assert "UNSCREENED" in out and "tesseract not installed" in out and host.NO_PROFILE in out
    assert run(["extract", f"{INBOX}/photo.jpg", "--pages", "1"]) == 0
    (ex,) = (rec / STATE / "extracts").iterdir()
    body = ex.read_text()
    assert "tesseract not installed" in body and "(no text could be read" in body


def test_sniff_by_magic_not_extension(tmp_path):
    p = tmp_path / "looks.pdf"
    p.write_bytes(b"\x89PNG\r\n\x1a\n....")
    assert reader.sniff(str(p)) == "png"
    p.write_bytes(b"%PDF-1.4\n")
    assert reader.sniff(str(p)) == "pdf"
    p.write_bytes(b"PK\x03\x04....")
    assert reader.sniff(str(p)) == "zip"


# ---------------------------------------------------------------- a real PDF through pdftotext
def make_pdf(path, lines):
    """A one-page PDF with a Courier text layer, so pdftotext -layout keeps the columns."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 9 Tf 11 TL 20 760 Td " + " ".join(f"({esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            + "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


W2_LINES = [
    "W-2 Wage and Tax Statement 2030",
    f"a Employee's social security number   {SSN}",
    "b Employer identification number (EIN)      1 Wages, tips, other compensation    2 Federal income tax withheld",
    f"{EIN}                                   52,000.00                            6,100.00",
    "                                            3 Social security wages              4 Social security tax withheld",
    "                                            54,000.00                            3,348.00",
    "                                            5 Medicare wages and tips            6 Medicare tax withheld",
    "                                            54,000.00                            783.00",
    "d Control number                            12a See instructions for box 12",
    "A1B2C3                                      D  2,000.00",
    "15 State  Employer's state ID number   16 State wages, tips, etc.   17 State income tax",
    "XX 1234567                             52,000.00                    2,500.00",
]


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext not installed")
def test_w2_pdf_fields(rec):
    p = inbox(rec) / "w2.pdf"
    make_pdf(p, W2_LINES)
    assert run(["fields", "--task", "tax_w2", f"{INBOX}/w2.pdf"]) == 0
    (t,) = (rec / STATE / "tasks").iterdir()
    task = json.loads(t.read_text())
    v = {r["field"]: r["value"] for r in task["values"]}
    assert v["box1_wages"] == "52,000.00" and v["box2_federal_income_tax_withheld"] == "6,100.00"
    assert v["box6_medicare_tax_withheld"] == "783.00" and v["box12a"] == "D 2,000.00"
    assert v["box17_state_income_tax"] == "2,500.00"
    raw = t.read_text()
    for bad in (SSN, EIN, "A1B2C3", "1234567"):
        assert bad not in raw
    assert task["approved"] is False and mode(t) == 0o600
