"""Task files: profile fields pulled from documents, previewed and approved by the user, then read with a reason.

`fields` reads each named document in a bounded child, refuses one holding secret material (holding it when it is
in the inbox), extracts only the profile's allowlisted fields (fields.py), records where each value sits on its
page (locate.py) and writes <state>/tasks/<profile>-<date>.json, unapproved. `approve` asks the user on the
desktop (approval.py) and seals the content with a hash; `read` prints an approved, unchanged task file (or
extract) whose source documents are unchanged, and logs the read; `status` lists task files with counts and no
values; `keep`, `cleanup` and `extract` (a redacted extract of named pages, only when the user asks) complete it.
Stdlib only.
"""
import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter

from . import approval, detect, fields as fx, llm, locate, places, preview, reader
from .config import CFG
from .dates import document_date
from .places import DocError, clean_part, inbox, inside, load_index, log_access, rel, root, save_index, state_dir
from .places import write_private

from .scan import HELD, hold

WARNING = ("WARNING: redaction is pattern-based and can miss things: names, addresses and phone numbers in other\n"
           "#   layouts than the detectors know, and numbers broken up by OCR errors, are not caught. The model pass,\n"
           "#   when used, is best-effort and nondeterministic.")


def resolve_record(name):
    """A document under the records root (not the state folder, not held)."""
    cands = [name] if os.path.isabs(name) else [os.path.join(root(), name), os.path.join(inbox(False), name), name]
    for p in cands:
        if os.path.isfile(p):
            break
    else:
        raise DocError(f"no such file under the records folder: {name}")
    if not inside(p, root()):
        raise DocError(f"only files under {CFG.root_name} are read")
    if inside(p, os.path.join(root(), CFG.state)) or inside(p, os.path.join(inbox(False), HELD)):
        raise DocError("held files and the intake's own files are never read")
    return os.path.realpath(p)


def _refuse_secret(p, counts):
    if detect.has_credentials(counts):
        if inside(p, inbox(False)):
            idx = load_index()
            hold({"path": p, "name": os.path.basename(p), "sha256": places.sha256(p), "counts": counts}, idx)
            save_index(idx)
        raise DocError(f"{os.path.basename(p)} holds secret material ({detect.fmt_counts(counts)}): {CFG.held_note}")


def _task_path(task):
    d = state_dir("tasks")
    base = os.path.join(d, f"{task}-{dt.date.today().isoformat()}")
    p, n = base + ".json", 2
    while os.path.exists(p):
        p, n = f"{base}-{n}.json", n + 1
    return p


def missing_fields(prof, records, plans):
    have = {(r["group"], r["plan"], r["field"]) for r in records}
    out = []
    for f in prof.get("fields", []):
        g = f["group"]
        targets = plans.get(g) or [None] if prof["groups"][g].get("per_plan") else [None]
        for p in targets:
            if f["name"] != "plan_name" and (g, p, f["name"]) not in have:
                out.append({"group": g, "plan": p, "field": f["name"], "type": f["type"]})
    return out


def _apply_model(prof, wanted, cands, records, plans, dropped, src_of_page):
    allowed = {(f["group"], f["name"]): f["type"] for f in prof.get("fields", [])}
    want = {(w["group"], w["plan"], w["field"]) for w in wanted}
    added = 0
    for pno, g, plan, field, value in cands:
        if (g, plan, field) not in want or (g, field) not in allowed:
            continue  # only fields that were asked for, and only profile fields
        ftype = allowed[(g, field)]
        if plan is not None and plan not in plans.get(g, []):
            continue
        if not fx.valid_value(ftype, value):
            continue
        value = fx.normalize(ftype, value)
        kind = fx.sensitive(value, field)
        if kind:
            dropped[kind] = dropped.get(kind, 0) + 1
            continue
        records.append({"group": g, "plan": plan, "row": None, "field": field, "value": value,
                        "file": src_of_page, "page": pno, "label": "local model"})
        want.discard((g, plan, field))
        added += 1
    return added


def typed_records(prof, values):
    types = fx.allowed_fields(prof)
    return [{**v, "type": types.get((v["group"], v["field"]), "")} for v in values]


def task_summary(t, prof=None):
    """Counts only: documents, fields, missing (required), flagged values and documents, complete documents."""
    try:
        prof = prof or fx.load_profile(t["task"])
    except fx.ProfileError:
        prof = {"fields": [], "groups": {}}
    required = {f["name"] for f in prof.get("fields", []) if f.get("required")}
    docs_, flagged_docs, flagged, complete, req_missing = 0, 0, 0, 0, 0
    for src in t.get("sources", []):
        docs_ += 1
        recs = typed_records(prof, [v for v in t["values"] if v.get("file") == src["file"]])
        fl = fx.document_flags(recs)
        flagged += len(fl)
        flagged_docs += bool(fl)
        have = {r["field"] for r in recs}
        miss = required - have
        req_missing += len(miss)
        complete += not miss
    return {"documents": docs_, "fields": len(t.get("values", [])), "missing": len(t.get("missing", [])),
            "missing_required": req_missing, "flagged": flagged, "flagged_documents": flagged_docs,
            "complete": complete}


def _replace_target(old, task_name):
    """fields --replaces OLD: (path to write, old path to mark superseded or None). An unapproved OLD is
    overwritten in place; an approved one is kept, marked superseded, and the new file gets a -vN name."""
    p, kind = resolve_output(old)
    if kind != "task":
        raise DocError("--replaces names a task file")
    t = _load_task(p)
    if t.get("task") != task_name:
        raise DocError(f"{os.path.basename(p)} is a {t.get('task')} task, not {task_name}")
    if t.get("superseded_by"):
        raise DocError(f"{os.path.basename(p)} is already superseded by {t['superseded_by']}")
    if t.get("approved") is not True:
        return p, None
    stem = re.sub(r"-v\d+$", "", p[:-5])
    n = 2
    while os.path.exists(f"{stem}-v{n}.json"):
        n += 1
    return f"{stem}-v{n}.json", p


def cmd_labels(prof, args, out=print):
    """Debug: per file and page, which profile labels and known senders matched, with line and column. Never a
    value, never the line's text: only whether a value of the field's type follows on the line or below."""
    fields = fx._compile(prof)
    for name in args.files:
        p = resolve_record(name)
        doc = reader.read_bounded_or_fail(p)
        _refuse_secret(p, reader.sensitive_counts(doc))
        kv = fx.known_vendor(doc["pages"])
        out(f"{os.path.basename(p)}: {doc['total']} pages, text {doc['method']}; known sender: "
            + (f"yes ({kv[2]}, p.{kv[1]})" if kv else "no"))
        red = detect.redact("\n".join(t for _, t in doc["pages"]))[0]
        soon = CFG.today() + dt.timedelta(days=60)
        kinds = []
        for m in re.finditer(fx.DATE, red, re.I):
            d = fx.parse_date(m.group(0))
            if d:
                far = d > soon.isoformat()
                ren = bool(fx._RENEWAL.search(red[max(0, m.start() - 40):m.start()]))
                kinds.append(("far" if far else "near") + ("+renewal" if ren else ""))
        dd = document_date(red)
        out(f"  dates in the redacted text, in order: {', '.join(kinds) or 'none'}; scan's document date: "
            + ("none" if not dd else ("far-future" if dd > soon.isoformat() else "past or near")))
        for pno, text in doc["pages"]:
            for st, en, kind, _ in detect.find_spans(text):
                ls = text.rfind("\n", 0, st) + 1
                near = text[max(0, st - 60):min(len(text), en + 60)]
                ln_no = text.count(chr(10), 0, st)
                hdr = mail_header_of(text.split("\n"), ln_no)
                out(f"  p.{pno} line {ln_no + 1} col {st - ls + 1}: detector {kind} after "
                    f"\"{label_words(text[ls:st])}\"" + (f" in mail header {hdr}" if hdr else "")
                    + f"; length {en - st}; routing/bank word within 60 chars: "
                    + ("yes" if re.search(r"(?i)\b(?:routing|aba|bank|transit|rtn)\b", near) else "no"))
            lines = text.split("\n")
            for i, line in enumerate(lines):
                for f in fields:
                    for _, lab, _, rx in f["_labels"]:
                        for m in rx.finditer(line):
                            after = bool(fx._typed(f["type"], line[m.end():])) if f["type"] not in fx.WHOLE else \
                                bool(fx._cells(line[m.end():]))
                            below = any(fx._typed(f["type"], ln) for ln in lines[i + 1:i + 4]) \
                                if f["type"] not in fx.WHOLE else None
                            rng = bool(fx.DATE_RANGE.search(text, offs_of(lines, i) + m.end(),
                                                            offs_of(lines, i) + m.end() + 80))
                            col = column_below(f["type"], lines, i, m.start(), m.end()) \
                                if f["type"] not in fx.WHOLE and not after else None
                            out(f"  p.{pno} line {i + 1} col {m.start() + 1}: {f['name']} <- \"{lab}\" "
                                f"(type {f['type']}; value on line: {'yes' if after else 'no'}"
                                + ("" if below is None else f"; below: {'yes' if below else 'no'}")
                                + ("" if col is None else f"; in its column: {col}")
                                + ("; date range after: yes" if rng else "") + ")")
    return 0


def column_below(ftype, lines, i, start, end, most=12):
    """For --labels: how far below the label (in non-empty lines) the first value of its type sits in the label's
    column window (the one fields' below-lookup reads), as "N non-empty lines down", or "none within 12 lines"."""
    lo, hi = max(0, start - 4), max(end, start + 24) + 4
    seen = 0
    for ln in lines[i + 1:i + 1 + most]:
        if not ln.strip():
            continue
        seen += 1
        if fx._typed(ftype, ln[lo:hi]):
            return f"{seen} non-empty line{'s' if seen > 1 else ''} down"
    return f"none within {most} lines"


LABEL_WORDS = {"receipt", "transaction", "trans", "txn", "reference", "ref", "order", "confirmation", "conf",
               "number", "no", "num", "id", "ticket", "permit", "invoice", "auth", "authorization", "approval",
               "approved", "code", "payment", "routing", "aba", "bank", "account", "acct", "seq", "sequence",
               "terminal", "merchant", "station", "lot", "zone", "space", "session", "citation", "batch", "record",
               "tracking", "booking", "reservation", "member", "customer", "card", "check", "phone", "tel", "date",
               "time", "total", "amount", "tax", "rate", "location", "transit", "trace", "rrn", "stan", "mid", "tid"}


def label_words(before):
    """The last words before a value, kept only when they are common label words; anything else is '?'."""
    words = re.findall(r"[A-Za-z]+|#", before)[-3:]
    return " ".join(w.lower() if w.lower() in LABEL_WORDS or w == "#" else "?" for w in words) or "-"


_HEADER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,60}):")


def mail_header_of(lines, i):
    """The header name (Received, Message-ID, ...) when line i is in an email's header block, else None."""
    if not lines or not _HEADER_LINE.match(lines[0]):
        return None
    for j in range(i + 1):
        if not lines[j].strip("\r"):
            return None  # past the blank line that ends the headers
    for j in range(i, -1, -1):
        m = _HEADER_LINE.match(lines[j])
        if m:
            return m.group(1)
    return None


def offs_of(lines, i):
    return sum(len(x) + 1 for x in lines[:i])


def cmd_fields(args, out=print):
    prof = fx.load_profile(args.task)
    if getattr(args, "labels", False):
        return cmd_labels(prof, args, out)
    target, old = _replace_target(args.replaces, prof["name"]) if getattr(args, "replaces", None) else (None, None)
    allowed = fx.allowed_fields(prof)
    task = {"task": prof["name"], "created": dt.datetime.now().isoformat(timespec="seconds"), "approved": False,
            "keep": False, "profile_sha256": hashlib.sha256(
                open(fx.profile_path(prof["name"]), "rb").read()).hexdigest(),
            "sources": [], "values": [], "missing": [], "dropped": {}, "model": "off"}
    model = args.model or CFG.model()
    for name in args.files:
        p = resolve_record(name)
        doc = reader.read_bounded_or_fail(p)
        counts = reader.sensitive_counts(doc)
        _refuse_secret(p, counts)
        src = rel(p)
        records, dropped, plans = fx.extract(prof, doc["pages"], src, keep_span=True)
        where, _ = reader.run_bounded(locate.locate_values, p, reader.sniff(p), doc["pages"], records)
        for i, r in enumerate(records):
            r.pop("span", None)
            if where and where.get(i):
                r["where"] = where[i]
        wanted = missing_fields(prof, records, plans)
        if args.llm:
            red_pages = [(n, detect.redact(t)[0]) for n, t in doc["pages"]]
            cands, status = llm.fill_fields(red_pages, wanted, model)
            _apply_model(prof, wanted, cands, records, plans, dropped, src)
            task["model"] = status
            wanted = missing_fields(prof, records, plans)
        for r in records:
            if (r["group"], r["field"]) not in allowed:
                raise DocError(f"internal: {r['group']}.{r['field']} is not in profile {prof['name']}")
        sug = CFG.route(p, reader.sniff(p), detect.redact("\n".join(t for _, t in doc["pages"]))[0])
        task["sources"].append({"file": src, "sha256": places.sha256(p), "pages": doc["total"], "text": doc["method"],
                                "suggest": {"dest": sug["dest"], "name": sug["name"]}})
        task["values"] += records
        task["missing"] += [f"{w['group']}.{w['field']}" + (f" ({w['plan']})" if w["plan"] else "") for w in wanted]
        for k, n in dropped.items():
            task["dropped"][k] = task["dropped"].get(k, 0) + n
        if doc["notes"]:
            out(f"{os.path.basename(p)}: " + "; ".join(doc["notes"]))
    path = target or _task_path(prof["name"])
    if old:
        task["replaces"] = os.path.basename(old)
    elif target and _load_task(target).get("replaces"):  # re-running a -v2: it still replaces the first file
        task["replaces"] = _load_task(target)["replaces"]
    write_private(path, json.dumps(task, indent=1))
    if old:  # the approved-but-wrong file is never read again
        t = _load_task(old)
        t["superseded_by"] = os.path.basename(path)
        t["superseded_at"] = dt.datetime.now().isoformat(timespec="seconds")
        write_private(old, json.dumps(t, indent=1))
        log_access("supersede", rel(old), "superseded_by=" + os.path.basename(path))
        out(f"superseded: {os.path.basename(old)} (approved earlier; read and file now refuse it)")
    elif target:
        out(f"replaced: {os.path.basename(target)} (was not approved)")
    c = task_summary(task, prof)
    out(f"wrote {rel(path)}: {c['documents']} documents, {len(task['values'])} fields, {len(task['missing'])} missing "
        f"({c['missing_required']} required), {c['flagged']} flagged in {c['flagged_documents']} documents, "
        f"{c['complete']} complete, {sum(task['dropped'].values())} dropped as sensitive "
        f"({detect.fmt_counts(task['dropped'])}); approved: no")
    if places.is_tty():
        preview.preview_table(task, out)
    else:
        out(f"The user previews it in their own terminal: {CFG.cli} preview \"{os.path.basename(path)}\"")
        out(f"or as a page: {CFG.cli} preview --html \"{os.path.basename(path)}\"")
        out(f"then, once they have looked: {CFG.cli} approve \"" + os.path.basename(path) + "\"")
    return 0



def resolve_output(name):
    """A task JSON or an extract in the state folder."""
    cands = [name] if os.path.isabs(name) else [os.path.join(state_dir("tasks"), name),
                                                os.path.join(state_dir("extracts"), name),
                                                os.path.join(root(), name), name]
    for p in cands:
        if os.path.isfile(p) and inside(p, os.path.join(root(), CFG.state)) and (
                inside(p, state_dir("tasks")) or inside(p, state_dir("extracts"))):
            kind = "task" if p.endswith(".json") else "extract"
            return os.path.realpath(p), kind
    raise DocError(f"no task file or extract named {name}")


def _load_task(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _header(p):
    out = {}
    with open(p, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("# "):
                break
            if line.startswith("# ----"):
                break
            k, _, v = line[2:].partition(":")
            out.setdefault(k.strip(), v.strip())
    return out


def _set_header(p, key, value):
    with open(p, encoding="utf-8") as f:
        lines = f.read().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# ----"):
            break
        if line.startswith(f"# {key}:"):
            lines[i] = f"# {key}: {value}"
    write_private(p, "\n".join(lines))


def approval_text(t, name):
    """The confirmation dialog's text: task, documents, fields and the money totals per group, flagged count."""
    c = task_summary(t)
    try:
        types = fx.allowed_fields(fx.load_profile(t["task"]))
    except (fx.ProfileError, KeyError):
        types = {}
    groups = {}
    for v in t.get("values", []):
        g = groups.setdefault(v.get("group", "?"), [0, 0.0, False])
        g[0] += 1
        if "total" in v.get("field", "") and types.get((v.get("group"), v.get("field"))) == "money":
            try:
                g[1] += float(_amount(v.get("value")))
                g[2] = True
            except ValueError:
                pass
    rows = [(g, n, f"{tot:,.2f}" if has else None) for g, (n, tot, has) in sorted(groups.items())]
    return approval.summary(f"{t.get('task', '?')} ({name})", "task", c["documents"], rows, c["flagged"])


def _confirm(args, text):
    try:
        return approval.confirm("Approve document fields", text, terminal=getattr(args, "terminal_confirm", False))
    except approval.ApprovalError as e:
        raise DocError(str(e)) from e


def cmd_approve(args, out=print):
    if not places.is_tty():  # defence in depth: the desktop dialog below is the real check
        raise DocError(f"approve is for the user's own terminal, after preview; {CFG.assistant} never approves its "
                       "own reads")
    p, kind = resolve_output(args.target)
    name = os.path.basename(p)
    if kind == "task":
        t = _load_task(p)
        if t.get("superseded_by"):
            raise DocError(f"not approved: {name} is superseded by {t['superseded_by']}")
        how = _confirm(args, approval_text(t, name))
        t.update(approval.seal_task(t))
        write_private(p, json.dumps(t, indent=1))
        try:
            approval.check_task(_load_task(p), name)
        except approval.ApprovalError as e:
            raise DocError(f"approval of {name} did not save") from e
        log_access("approve", rel(p), f"{kind} via={how}")
        out(f"approved: {name} ({len(t.get('sources', []))} documents, {len(t['values'])} fields)")
        return 0
    h = _header(p)
    if h.get("needs-review") == "yes":
        out("note: this extract is marked needs-review: the user has checked its [LLM-FLAGGED] spans")
    how = _confirm(args, approval.summary(name, "extract", 1, [(f"pages {h.get('pages', '?')}", None, None)],
                                          h.get("llm-flags", "none"), question=approval.QUESTION_EXTRACT))
    with open(p, encoding="utf-8") as f:
        write_private(p, approval.seal_extract(f.read()))
    log_access("approve", rel(p), f"{kind} via={how}")
    out(f"approved: {name} (extract, pages {_header(p).get('pages', '?')})")
    return 0


def approved_word(t):
    """status's approved column: yes only when the approval hash still matches (sources are checked by read)."""
    if t.get("approved") is not True:
        return "no"
    if not t.get("approved_hash"):
        return "no hash"
    return "yes" if approval.task_digest(t) == t["approved_hash"] else "CHANGED"


def cmd_status(args, out=print):
    """Task files with approved yes/no, superseded, counts and created dates. No values."""
    d = state_dir("tasks")
    rows = [("task file", "profile", "approved", "docs", "fields", "missing", "req. missing", "flagged docs",
             "complete", "created", "note")]
    for n in sorted(os.listdir(d)):
        if not n.endswith(".json"):
            continue
        try:
            t = _load_task(os.path.join(d, n))
        except (OSError, ValueError):
            rows.append((n, "?", "?", "", "", "", "", "", "", "", "unreadable"))
            continue
        c = task_summary(t)
        note = f"superseded by {t['superseded_by']}" if t.get("superseded_by") else (
            f"replaces {t['replaces']}" if t.get("replaces") else "")
        rows.append((n, t.get("task", "?"), approved_word(t), str(c["documents"]),
                     str(c["fields"]), str(c["missing"]), str(c["missing_required"]), str(c["flagged_documents"]),
                     str(c["complete"]), (t.get("created") or "?")[:16].replace("T", " "), note))
    if len(rows) == 1:
        out("no task files")
        return 0
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        out("  ".join(x.ljust(w[i]) for i, x in enumerate(r)).rstrip())
    if getattr(args, "detail", False):  # field names and flag reasons with counts, never values
        for n in sorted(os.listdir(d)):
            if not n.endswith(".json"):
                continue
            try:
                t = _load_task(os.path.join(d, n))
                prof = fx.load_profile(t["task"])
            except (OSError, ValueError, KeyError, fx.ProfileError):
                continue
            miss = Counter(t.get("missing", []))
            why = Counter()
            for src in t.get("sources", []):
                recs = typed_records(prof, [v for v in t["values"] if v.get("file") == src["file"]])
                for i, reason in fx.document_flags(recs).items():
                    why[f"{recs[i]['field']}: {reason}"] += 1
            labs = Counter(f"{v['field']} <- {v['label']}" for v in t.get("values", []))
            loc = Counter("box" if (v.get("where") or {}).get("box") else "line" if (v.get("where") or {}).get("span")
                          else "none" for v in t.get("values", []))
            out(f"{n}: missing " + (", ".join(f"{k} {c}" for k, c in sorted(miss.items())) or "none")
                + "; flagged " + (", ".join(f"{k} ({c})" for k, c in sorted(why.items())) or "none")
                + "; matched by " + ", ".join(f"{k} {c}" for k, c in sorted(labs.items()))
                + "; located: " + ", ".join(f"{k} {c}" for k, c in sorted(loc.items())))
    return 0


def _source_path(src, idx):
    """Where a task's or extract's source document is now: its recorded path, else where the index says it was
    filed (same sha256). None when neither exists."""
    for relp in (src.get("file"), (idx.get("files", {}).get(src.get("sha256")) or {}).get("path")):
        if relp and os.path.isfile(os.path.join(root(), relp)):
            return os.path.join(root(), relp)
    return None


def check_approved(t, name):
    """DocError unless the task is approved, unchanged since, and its source documents are unchanged."""
    try:
        approval.check_task(t, name)
        idx = load_index()
        approval.check_sources(t.get("sources", []), lambda s: _source_path(s, idx), places.sha256, name)
    except approval.ApprovalError as e:
        raise DocError(str(e)) from e


def cmd_read(args, out=print):
    if not args.reason or not args.reason.strip():
        raise DocError("read needs --reason")
    p, kind = resolve_output(args.target)
    name = os.path.basename(p)
    if kind == "task":
        t = _load_task(p)
        if t.get("superseded_by"):
            raise DocError(f"{name} is superseded by {t['superseded_by']}: read that one once approved")
        check_approved(t, name)
        want = {f.strip() for f in args.fields.split(",")} if args.fields else None
        vals = [{k: v[k] for k in ("group", "plan", "row", "field", "value", "file", "page", "label") if k in v}
                for v in t["values"] if not want or v["field"] in want or f"{v['group']}.{v['field']}" in want]
        log_access("read", rel(p), "fields=" + ",".join(sorted({f"{v['group']}.{v['field']}" for v in vals})),
                   args.reason)
        out(json.dumps({"task": t["task"], "sources": [s["file"] for s in t["sources"]], "values": vals,
                        "missing": t["missing"], "dropped": t["dropped"]}, indent=1))
        return 0
    with open(p, encoding="utf-8") as f:
        text = f.read()
    h = _header(p)
    try:
        approval.check_extract(h, text, name)
        idx = load_index()
        approval.check_sources([{"file": h.get("source"), "sha256": h.get("sha256")}],
                               lambda s: _source_path(s, idx), places.sha256, name)
    except approval.ApprovalError as e:
        raise DocError(str(e)) from e
    if args.fields:
        raise DocError("--fields is for task files")
    log_access("read", rel(p), f"pages={h.get('pages', '?')}", args.reason)
    out(text.rstrip("\n"))
    return 0


def cmd_keep(args, out=print):
    p, kind = resolve_output(args.target)
    if kind == "task":
        t = _load_task(p)
        t["keep"] = True
        write_private(p, json.dumps(t, indent=1))
    else:
        _set_header(p, "keep", "yes")
    log_access("keep", rel(p), kind)
    out(f"kept (cleanup skips it): {rel(p)}")
    return 0


def cmd_cleanup(args, out=print):
    now = dt.datetime.now()
    limit = dt.timedelta(days=args.older_than)
    gone, kept = [], 0
    for p in sorted(os.path.join(state_dir("tasks"), n) for n in os.listdir(state_dir("tasks"))):
        if not p.endswith(".json"):
            continue
        try:
            t = _load_task(p)
        except (OSError, ValueError):
            continue
        if args.task and t.get("task") != args.task:
            continue
        if t.get("keep"):
            kept += 1
            continue
        created = dt.datetime.fromisoformat(t.get("created", now.isoformat()))
        if now - created >= limit:
            gone.append(p)
    if not args.task:
        for n in sorted(os.listdir(state_dir("extracts"))):
            p = os.path.join(state_dir("extracts"), n)
            if not n.endswith(".txt"):
                continue
            if _header(p).get("keep") == "yes":
                kept += 1
                continue
            if now - dt.datetime.fromtimestamp(os.path.getmtime(p)) >= limit:
                gone.append(p)
    for p in gone:
        out(("would delete: " if args.dry_run else "deleted: ") + rel(p))
        if not args.dry_run:
            os.unlink(p)
    out(f"{len(gone)} {'would be ' if args.dry_run else ''}deleted, {kept} kept")
    if not args.dry_run and gone:
        log_access("cleanup", f"{len(gone)} files", f"task={args.task or 'all'} older-than={args.older_than}d")
    return 0


# ---------------------------------------------------------------- command: extract
def parse_pages(spec):
    pages = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise DocError(f"--pages takes numbers and ranges such as 1-2,4, not {part!r}")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b < a or b - a > 200:
            raise DocError(f"bad page range {part!r}")
        pages.update(range(a, b + 1))
    if not pages:
        raise DocError("extract needs --pages (for example --pages 1-2)")
    return sorted(pages)


def _ranges(pages):
    out, start, prev = [], None, None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else str(start))
    return ",".join(out)


def build_extract(p, pages, llm_check=False, model=None):
    """(header+text, counts, needs_review). Screens the whole file for secrets first."""
    full = reader.read_bounded_or_fail(p, whole=True)
    counts = reader.sensitive_counts(full)
    _refuse_secret(p, counts)
    doc = reader.read_bounded_or_fail(p, pages=pages, barcodes=False)
    got = [n for n, _ in doc["pages"]]
    body, red = [], Counter()
    for n, text in doc["pages"]:
        t, c = detect.redact(text)
        red.update(c)
        body.append(f"--- page {n} ---\n{t.rstrip()}")
    text = "\n\n".join(body) if any(t.strip() for _, t in doc["pages"]) else ""
    status, flags = "off", {}
    if llm_check and text:
        text, flags, status = llm.flag_spans(text, model)
    needs = bool(flags) or status.startswith("failed")
    head = ["# doc-intake redacted extract", f"# source: {rel(p)}", f"# sha256: {places.sha256(p)}",
            f"# pages: {_ranges(got) or 'none'} of {full['total']}", f"# text: {doc['method']}",
            f"# redacted: {detect.fmt_counts(dict(red))}", f"# llm-check: {status}",
            f"# llm-flags: {detect.fmt_counts(flags) if flags else 'none'}",
            f"# needs-review: {'yes' if needs else 'no'}", "# approved: no", "# keep: no",
            f"# created: {dt.datetime.now().isoformat(timespec='seconds')}", "# " + WARNING]
    if doc["notes"]:
        head.append("# notes: " + "; ".join(doc["notes"]))
    if not text.strip():
        text = "(no text could be read from these pages)"
    return "\n".join(head) + "\n# ----\n" + text + "\n", dict(red), needs, flags, status


def cmd_extract(args, out=print):
    pages = parse_pages(args.pages)
    model = args.model or CFG.model()
    for name in args.files:
        p = resolve_record(name)
        content, red, needs, flags, status = build_extract(p, pages, args.llm_check, model)
        stem = clean_part(os.path.splitext(os.path.basename(p))[0])
        dest = os.path.join(state_dir("extracts"), f"{places.sha256(p)[:8]} - {stem} p{_ranges(pages)}.txt")
        write_private(dest, content)
        log_access("extract", rel(p), f"pages={_ranges(pages)}", "user asked")
        out(f"wrote {rel(dest)}: redacted {detect.fmt_counts(red)}; llm-check {status}; "
            f"needs-review: {'yes' if needs else 'no'}; approved: no")
        out(f"The user looks at it in their own editor, then: {CFG.cli} approve \""
            + os.path.basename(dest) + "\"")
    return 0


def _amount(v):
    return re.sub(r"[$,\s]", "", v or "")


def approved_task_for(digest, kinds=("receipt", "invoice")):
    """(path, task) of the newest approved, unchanged task file of one of kinds with a source of this sha256, the
    task narrowed to that one source and its values; or None: what a host's filing step files from."""
    d = state_dir("tasks")
    best = None
    for n in sorted(os.listdir(d)):
        if not n.endswith(".json"):
            continue
        try:
            t = _load_task(os.path.join(d, n))
        except (OSError, ValueError):
            continue
        if not (t.get("task") in kinds and t.get("approved") is True and not t.get("superseded_by")
                and approval.task_digest(t) == t.get("approved_hash")):
            continue
        # a task may cover a batch of documents: take this document's source and only its values
        src = next((s for s in t.get("sources", []) if s.get("sha256") == digest), None)
        if src is None:
            continue
        one = dict(t, sources=[src], values=[x for x in t.get("values", []) if x.get("file") == src.get("file")])
        if best is None or t.get("created", "") >= best[1].get("created", ""):
            best = (os.path.join(d, n), one)
    return best


def _task_files():
    """[(task file name, task)] for every readable task file."""
    d, out = state_dir("tasks"), []
    for n in sorted(os.listdir(d)):
        if n.endswith(".json"):
            try:
                out.append((n, _load_task(os.path.join(d, n))))
            except (OSError, ValueError):
                continue
    return out


