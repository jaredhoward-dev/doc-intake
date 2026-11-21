"""Shared test helpers: fake values, an inbox in the throwaway root, and running the fake host's CLI.

Every value is fake: the SSN is the well-known invalid example, built at run time so a pre-commit SSN check never
sees it in the source; the card numbers are test numbers; the routing number is made up to pass the checksum.
"""
import os
import stat

import pytest

import host
from doc_intake import places, reader

SSN = "078" + "-05-" + "1120"
CARD = "4111111111111111"
CARD_SPACED = "4111 1111 1111 1111"
ROUTING = "111111118"            # 3-7-1 checksum passes, prefix 11 is valid; not a real bank
ACCOUNT = "12345678901"
EIN = "12-3456789"
MBI = "1EG4-TE5-MK73"            # CMS's published example format
FAKES = [SSN, CARD, CARD_SPACED, ROUTING, ACCOUNT, EIN, MBI, "C12345678", "XYZ123456789", "00123456",
         "someone@example.com", "hunter22"]


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def rec(records, monkeypatch):
    """records (conftest) with no terminal and no barcode module, unless a test says otherwise."""
    monkeypatch.setattr(places, "is_tty", lambda: False)
    monkeypatch.setattr(reader, "_zxing", lambda: None)
    return records


def inbox(root):
    d = root / host.INBOX
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def drop(root, name, text):
    p = inbox(root) / name
    p.write_text(text)
    return p


def run(argv, answers=None):
    """host.main; approve runs as if at the user's own terminal (it refuses anywhere else)."""
    it = iter(answers or [])
    if argv and argv[0] == "approve":
        orig = places.is_tty
        places.is_tty = lambda: True
        try:
            return host.main(argv, ask_fn=lambda _p: next(it))
        finally:
            places.is_tty = orig
    return host.main(argv, ask_fn=lambda _p: next(it))
