"""The fake host itself: it configures the engine, routes the four toy kinds, and runs a scan end to end."""
import host


def test_classify_toy_kinds():
    assert host.classify("Invoice\nBill to: someone\nAmount due 10.00")[0] == "invoice"
    assert host.classify("Receipt\nTotal 4.00")[0] == "receipt"
    assert host.classify("hello world") == ("unknown", 0)


def test_scan_runs_on_an_empty_inbox(records, capsys):
    (records / host.INBOX).mkdir(mode=0o700)
    assert host.main(["scan"]) == 0
