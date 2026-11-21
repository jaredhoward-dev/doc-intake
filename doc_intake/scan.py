"""Screening the inbox: type, pages, sensitive counts, secret material held, exact and near duplicates, routing.

screen() reads one inbox file in a bounded child, counts what the detectors find (never values), holds a file
with secret material (passwords, keys, 2FA codes) in the inbox's .held/ folder, matches it against the records'
sha256 and text fingerprints, and asks the host's route hook where it would go. cmd_scan runs it over the whole
inbox and prints a table of names, types, page counts, detector counts, destinations and suggested names.

--llm-names: for unknown or generic documents, a short title from the local model, kept only when safe_title
passes it (2-6 plain words, no long number, nothing a detector flags, no private term, no word that follows a
person label in the text). Titles kept are cached by sha256 in <state>/titles.json. Stdlib only.
"""
import datetime as dt
import json
import os
import re
import resource
import sys

from . import detect, fields as fx, fingerprints, llm, places, reader, similar
from .config import CFG
from .fingerprints import page_text_fp, profile_line, _mb
from .places import clean_part, inbox, load_index, move_no_overwrite, rel, save_index, state_dir, write_private

HELD = ".held"
NO_PROFILE = "no profile: ask the user which fields are needed"


class Uncapped:
    """Every command that reads documents says once, at its end, how many files it read without the memory cap
    (the platform refused it), in this one wording; counts only. Creating one resets reader's count, so each
    command starts from zero. file_done() after each file counts it once however many bounded reads it took;
    note() counts any reads since the last file_done() as one file each (scan, preview: one read per file)."""

    def __init__(self):
        reader.reset_uncapped()
        self.files = 0
        self._seen = 0

    def file_done(self):
        n = reader.uncapped_reads()
        self.files += n > self._seen
        self._seen = n

    def note(self, out):
        n = self.files + reader.uncapped_reads() - self._seen
        if n:
            out(f"\n{n} {'file' if n == 1 else 'files'} read without a memory cap (this platform refuses it); the "
                "timeout and pixel caps still applied.")


def screen(path, idx, known=None, batch=None, near=None, title_fn=None, route_opts=None):
    """known: {sha256: path} of the files already in the records (records_index); batch: {sha256: path} of the
    inbox files screened earlier in this run. A match in either makes this file a duplicate. near: [(path, text
    fingerprint)] of records files and earlier inbox files: a close one marks it a possible duplicate (near_of,
    similarity), still filable. title_fn(redacted) -> a checked title or None, handed to the route hook for a new
    file. route_opts: keyword options for the route hook (the host's own, such as a year). The text stays in this
    function."""
    ftype = reader.sniff(path)
    digest = places.sha256(path)
    doc, info = reader.read_document_bounded(path, ftype)
    counts = reader.sensitive_counts(doc)
    text = "\n".join(t for _, t in doc["pages"])
    redacted, _ = detect.redact(text)
    r = {"path": path, "name": os.path.basename(path), "type": ftype, "size": os.path.getsize(path),
         "sha256": digest, "pages": doc["total"], "method": doc["method"], "notes": doc["notes"], "counts": counts,
         "held": detect.has_credentials(counts), "mem": info, "unread": doc.get("unread"),
         "incomplete": doc.get("incomplete")}
    prior = idx["files"].get(digest)
    if r["held"]:
        r["status"] = "held"
    elif prior and prior.get("status") in CFG.filed_statuses:
        r["status"] = "duplicate"
        r["dup_of"] = prior.get("path")
    elif known and digest in known:
        r["status"] = "duplicate"
        r["dup_of"] = known[digest]
    elif batch and digest in batch:
        r["status"] = "duplicate"
        r["dup_of"] = batch[digest]
        r["dup_in_batch"] = True
    elif ftype in ("zip", "unknown"):
        r["status"] = "unsupported"
    elif doc.get("incomplete"):
        r["status"] = "unscreened"  # never passed as safe on a partial read
    elif not text.strip():
        r["status"] = "unscreened"
    else:
        r["status"] = "new"
    # near-duplicates: advisory only, and only for a file that is neither held nor an exact copy
    r["text_fp"] = None if r["held"] else page_text_fp(doc["pages"])
    if r["text_fp"] and r["status"] in ("new", "unscreened") and near:
        m = similar.best_match(r["text_fp"], near)
        if m:
            r["near_of"], r["similarity"] = m[0], int(round(m[1] * 100))
    i = CFG.route(path, ftype, redacted, title_fn=title_fn if r["status"] == "new" else None, **(route_opts or {}))
    r.update(category=i["category"], profile=i["profile"], dest=i["dest"], suggested=i["name"], note=i["note"],
             date_from_file=i["date_from_file"], name_from_model=i["name_from_model"])
    if r["status"] in ("unscreened", "unsupported"):
        r.update(category="unknown", profile=None, note=NO_PROFILE)
    return r


def near_note(r):
    return f"possible duplicate of {r['near_of']} (similarity {r['similarity']}%)"


DATE_NOTE = "date: from file — check it"


def hold(r, idx, out=print):
    d = os.path.join(inbox(), HELD)
    os.makedirs(d, mode=0o700, exist_ok=True)
    dest = move_no_overwrite(r["path"], os.path.join(d, r["name"]))
    idx["files"][r["sha256"]] = {"status": "held", "path": rel(dest), "at": dt.date.today().isoformat(),
                                 "counts": r["counts"]}
    out(f"HELD {r['name']}: {CFG.held_note} (in {rel(d)}/; {detect.fmt_counts(r['counts'])})")
    return dest


def inbox_files():
    d = inbox()
    out = []
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if n.startswith(".") or os.path.islink(p) or not os.path.isfile(p):
            continue
        out.append(p)
    return out


def _trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def print_table(rows, out=print):
    head = ("#", "file", "type", "pages", "sensitive", "destination", "suggested name")
    data = []
    for i, r in enumerate(rows, 1):
        sens = detect.fmt_counts(r["counts"]) if r["status"] not in ("unscreened", "unsupported") else r["status"].upper()
        if r["status"] == "unscreened" and r.get("unread") == "too large":
            sens = "UNSCREENED (too large)"
        elif r["status"] == "unscreened" and r.get("unread"):
            sens = f"UNSCREENED ({r['unread']})"
        elif r["status"] == "unscreened" and r.get("incomplete"):
            sens = "UNSCREENED (partly read)"
        dest = r["dest"] or "-"
        if r["status"] == "duplicate":
            dest = f"duplicate of {r.get('dup_of')}"
        elif r["status"] == "held":
            dest = "held"
        data.append((str(i), _trunc(r["name"], 36), r["type"], str(r["pages"] or "-"), _trunc(sens, 36),
                     _trunc(dest, 48), _trunc(r["suggested"], 48)))
    widths = [max(len(x[c]) for x in [head] + data) for c in range(len(head))]
    for row in [head] + data:
        out("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())
    for i, r in enumerate(rows, 1):
        live = r["status"] not in ("held", "duplicate")
        bits = [near_note(r)] if live and r.get("near_of") else []
        bits += [r["note"]] if r["note"] and live else []
        bits += [DATE_NOTE] if live and r.get("date_from_file") else []
        bits += ["name from the local model: check it"] if live and r.get("name_from_model") else []
        bits += r["notes"]
        if bits:
            out(f"  {i}: " + "; ".join(bits))


def cmd_scan(args, out=print, route_opts=None):
    """Screen every inbox file (args: llm_names, model, profile_mem, retry_skipped); route_opts go to the route
    hook."""
    idx = load_index()
    files = inbox_files()
    if not files:
        out(f"inbox is empty: {rel(inbox())}/")
        return 0
    prof = _profiler(args)
    uncapped = Uncapped()
    known, near = fingerprints.records_index(out, retry_skipped=getattr(args, "retry_skipped", False), profile=prof)
    titles = load_titles()
    title_fn = _model_titles(args, out) if getattr(args, "llm_names", False) else None
    rows, batch = [], {}
    for p in files:
        digest = places.sha256(p)
        fn = title_fn
        if fn:
            fn = lambda red, fn=fn, d=digest: _remember_title(titles, d, fn(red))  # noqa: E731
        elif digest in titles:
            fn = lambda red, d=digest: titles[d]["title"]  # noqa: E731 - an earlier --llm-names title
        r = screen(p, idx, known, batch, near, fn, route_opts)
        if prof:
            prof(profile_line(rel(p), r["size"], r["type"], r["pages"], r["method"], r["mem"],
                              f"unscreened ({r['unread']})" if r.get("unread") else r["status"]))
        batch.setdefault(r["sha256"], rel(p))
        if r["held"]:
            hold(r, idx, out)
        elif r["text_fp"]:
            near = near + [(rel(p), r["text_fp"])]
        rows.append(r)
    save_index(idx)
    save_titles(titles)
    if prof:
        prof(f"mem: total  peak_rss_mb self={_mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)}  "
             f"children={_mb(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)}")
    print_table(rows, out)
    unread = sum(bool(r.get("unread") or r.get("incomplete")) for r in rows)
    if unread:
        out(f"\n{unread} {'file' if unread == 1 else 'files'} not fully read (too large, timeout or OCR failure): "
            "UNSCREENED, not checked for sensitive data. Look at them yourself before filing.")
    uncapped.note(out)
    dups = sum(r["status"] == "duplicate" for r in rows)
    if dups:
        out(f"\n{dups} exact {'copy' if dups == 1 else 'copies'} of a file already in {CFG.records_name} or earlier "
            "in this batch, left in the inbox; `file` won't file them. Remove them after checking, once the backup is "
            "verified.")
    nears = sum(bool(r.get("near_of")) and r["status"] not in ("held", "duplicate") for r in rows)
    if nears:
        out(f"\n{nears} possible {'duplicate' if nears == 1 else 'duplicates'} (similar text, not the same bytes): "
            "check them by name and date. `file` asks before filing one (--yes files it).")
    return 0


def _profiler(args):
    """scan --profile-mem: a function writing one line to stderr, or None."""
    if not getattr(args, "profile_mem", False):
        return None
    return lambda line: print(line, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- scan --llm-names: titles from the local model
TITLES = "titles.json"
TITLE_MAX_DIGITS = 4
_PERSON_LABEL = re.compile(r"(?i:\b(?:mr|mrs|ms|miss|mx|dr|dear|name|patient|insured|borrower|co-borrower|buyer|"
                           r"seller|purchaser|tenant|landlord|employee|member|owner|attn|attention|signed\s+by|"
                           r"prepared\s+for|client)\b)[.:,]?\s+((?:[A-Z][A-Za-z'-]+\s*){1,3})")


def titles_path():
    return os.path.join(state_dir(), TITLES)


def load_titles():
    try:
        with open(titles_path(), encoding="utf-8") as f:
            t = json.load(f)
        return t if isinstance(t, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_titles(titles):
    if titles or os.path.exists(titles_path()):
        write_private(titles_path(), json.dumps(titles, indent=1, sort_keys=True))


def _remember_title(titles, digest, title):
    if title:
        titles[digest] = {"title": title, "at": dt.date.today().isoformat()}
    return title


def person_words(text):
    """Lower-cased capitalized words right after a person label (Dear, Name:, Patient:, ...) in the text."""
    out = set()
    for m in _PERSON_LABEL.finditer(text or ""):
        out.update(w.lower() for w in re.findall(r"[A-Za-z'-]+", m.group(1)))
    return out


def safe_title(title, redacted):
    """The model's title if it is 2-6 plain words, no number longer than TITLE_MAX_DIGITS digits, nothing the
    detectors flag, no private term and no word that follows a person label in the text; else None."""
    if not isinstance(title, str):
        return None
    t = " ".join(title.split()).strip(" .,:;-")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9&' -]*", t):
        return None
    ws = t.split()
    if not 2 <= len(ws) <= 6 or re.search(r"\d{%d,}" % (TITLE_MAX_DIGITS + 1), t):
        return None
    if detect.find_spans(t) or fx.sensitive(t):
        return None
    low = t.lower()
    if any(term in low for term in CFG.privacy_terms()):
        return None
    if {w.lower() for w in re.findall(r"[A-Za-z'-]+", t)} & person_words(redacted):
        return None
    return clean_part(t) or None


def _model_titles(args, out):
    """A title function for the route hook, or None (with a note) when no model is set or the model is down."""
    model = getattr(args, "model", None) or CFG.model()
    if not model:
        out(f"--llm-names: no model ({CFG.model_hint}); names from file names")
        return None
    if not llm.reachable():
        out(f"--llm-names: {llm.not_reachable()}; names from file names")
        return None

    def fn(redacted):
        title, _status = llm.suggest_title(redacted, model)
        return safe_title(title, redacted)
    return fn

