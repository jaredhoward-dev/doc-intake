"""A small fake host for the tests: the engine configured the way a real host would, with a toy routing.

The engine knows no folder layout, so every test runs through this host, the same way a real one wraps the engine:
configure() points it at a throwaway records root ($DOC_INTAKE_TEST_ROOT, set by tests/conftest.py), the example
profiles and the fake known-senders map under tests/fixtures/, and a routing that knows four kinds of document
(receipt, invoice, pay stub, W-2). main() is the CLI a host would put in front of the engine's commands.

Everything here is test scaffolding: no real folder, vendor or rule.
"""
import argparse
import os
import re
import sys

import doc_intake
from doc_intake import fields as fx, pairs, preview, scan
from doc_intake.dates import document_date
from doc_intake.places import DocError, clean_part, log_access, rel, state_dir
from doc_intake.reader import EXT
from doc_intake.scan import NO_PROFILE
from doc_intake.tasks import (_task_files, cmd_approve, cmd_cleanup, cmd_extract, cmd_fields, cmd_keep, cmd_read,
                              cmd_status)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
PROFILES = os.path.join(FIXTURES, "profiles")
SENDERS = os.path.join(PROFILES, "senders.json")
INBOX = "inbox"
STATE = ".doc-intake"
CLI = "python3 tests/host.py"

_RULES = [
    ("tax_w2", [(r"\bform\s+w-?2\b|\bwage\s+and\s+tax\s+statement\b", 3)]),
    ("paystub", [(r"\bearnings\s+statement\b|\bpay\s*stub\b|\bpay\s+statement\b", 3),
                 (r"\bnet\s+pay\b|\bgross\s+pay\b|\bpay\s+date\b|\bpay\s+period\b|\bytd\b", 1)]),
    ("invoice", [(r"\binvoice\b", 2), (r"\bbill\s+to\b|\bamount\s+due\b|\bdue\s+date\b", 1)]),
    ("receipt", [(r"\breceipt\b", 2),
                 (r"\bamount\s+paid\b|\border\s+total\b|\btransaction\s+date\b|\bsubtotal\b|\btotal\b", 1)]),
]
_RULES = [(cat, [(re.compile(p, re.I), w) for p, w in pats]) for cat, pats in _RULES]
DEST = {"tax_w2": ("Tax/{y}", "Form W-2"), "paystub": ("Pay/{y}", "Pay stub"),
        "invoice": ("Expenses/{y}", "Invoice"), "receipt": ("Expenses/{y}", "Receipt")}


def root():
    return os.environ.get("DOC_INTAKE_TEST_ROOT", "")


def classify(text):
    """(category, score) from redacted text; ('unknown', 0) below a score of 2."""
    best = ("unknown", 0)
    for cat, pats in _RULES:
        score = sum(w for rx, w in pats if rx.search(text))
        if score > best[1]:
            best = (cat, score)
    return best if best[1] >= 2 else ("unknown", 0)


def route(path, ftype, redacted, title_fn=None, **opts):
    """Where a screened document would go: a toy layout by kind and year."""
    import datetime as dt
    cat, _ = classify(redacted)
    date = document_date(redacted)
    from_file = not date
    if from_file:
        date = dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()
    ext = EXT.get(ftype) or os.path.splitext(path)[1].lower()
    profile = cat if cat in DEST else None
    if profile:
        dest, what = DEST[cat]
        dest, note = dest.format(y=opts.get("year") or date[:4]), f"next: fields --task {profile}"
    else:
        dest, note = None, NO_PROFILE
        what = fx.normalize("short_text", os.path.splitext(os.path.basename(path))[0])[:60] or "Document"
    from_model = False
    if title_fn and redacted.strip() and cat == "unknown":
        title = title_fn(redacted)
        if title:
            what, from_model = title, True
    return {"category": cat, "profile": profile, "dest": dest, "name": f"{date} - {clean_part(what)}{ext}",
            "note": note, "date_from_file": from_file, "name_from_model": from_model}


def privacy_terms():
    """Terms a model-made title must never contain: a fixed fake list, so the check has something to catch."""
    return ["example family name"]


def configure():
    doc_intake.configure(
        root=root, inbox=lambda: INBOX, state=STATE, profiles_dir=PROFILES, senders_file=SENDERS, route=route,
        model=lambda: os.environ.get("DOC_INTAKE_MODEL", ""), privacy_terms=privacy_terms, cli=CLI,
        root_name="DOC_INTAKE_TEST_ROOT", assistant="the assistant")


configure()


def cmd_scan(args, out=print):
    return scan.cmd_scan(args, out, {"year": args.year})


def cmd_pairs(args, out=print):
    """Pairs of approved invoices and receipts, unmatched documents and mismatches (no bookkeeping here)."""
    docs_ = pairs.documents(_task_files())
    want = {os.path.basename(t) for t in args.tasks}
    keep = (lambda d: d["task"] in want) if want else (lambda d: True)  # noqa: E731
    found, left = pairs.match(docs_)
    found = [p for p in found if keep(p["invoice"]) or keep(p["receipt"])]
    left = [d for d in left if keep(d)]
    log_access("pairs", rel(state_dir("tasks")), "vendor,date,total,numbers", "pair agreement")
    bad = 0
    for p in found:
        out(pairs.describe(p))
        bad += not p["agree"]
    for d in left:
        out(pairs.describe_doc(d))
    out(f"{len(found)} pair(s), {bad} mismatch(es), {len(left)} unmatched")
    return 1 if bad else 0


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--year")
    s.add_argument("--llm-names", action="store_true")
    s.add_argument("--model")
    s.add_argument("--profile-mem", action="store_true")
    s.add_argument("--retry-skipped", action="store_true")
    s = sub.add_parser("fields")
    s.add_argument("--task", required=True)
    s.add_argument("files", nargs="+", metavar="FILE")
    s.add_argument("--llm", action="store_true")
    s.add_argument("--model")
    s.add_argument("--replaces", metavar="TASK_JSON")
    s.add_argument("--labels", action="store_true")
    s = sub.add_parser("preview")
    s.add_argument("target", metavar="TASK_JSON")
    s.add_argument("--html", action="store_true")
    s.add_argument("--no-open", action="store_true")
    s = sub.add_parser("pairs")
    s.add_argument("tasks", nargs="*", metavar="TASK_JSON")
    s = sub.add_parser("status")
    s.add_argument("--detail", action="store_true")
    for name in ("approve", "keep"):
        s = sub.add_parser(name)
        s.add_argument("target", metavar="TASK_JSON|EXTRACT")
        if name == "approve":
            s.add_argument("--terminal-confirm", action="store_true")
    s = sub.add_parser("read")
    s.add_argument("target", metavar="TASK_JSON|EXTRACT")
    s.add_argument("--reason", required=True)
    s.add_argument("--fields")
    s = sub.add_parser("extract")
    s.add_argument("files", nargs="+", metavar="FILE")
    s.add_argument("--pages", required=True)
    s.add_argument("--llm-check", action="store_true")
    s.add_argument("--model")
    s = sub.add_parser("cleanup")
    s.add_argument("--task")
    s.add_argument("--older-than", type=int, default=30, metavar="DAYS")
    s.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None, ask_fn=input):
    """The host's CLI: parse, then hand the command to the engine. ask_fn is unused (no interactive filing here);
    it is kept so tests written against a real host's main(argv, ask_fn) run unchanged."""
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        return {"scan": cmd_scan, "fields": cmd_fields, "preview": preview.cmd_preview, "approve": cmd_approve,
                "read": cmd_read, "keep": cmd_keep, "cleanup": cmd_cleanup, "extract": cmd_extract,
                "status": cmd_status, "pairs": cmd_pairs}[args.cmd](args)
    except (DocError, fx.ProfileError) as e:
        print(f"host: {e}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
