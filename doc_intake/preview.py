"""Previewing a task file: the terminal view, and the review page (preview --html).

The terminal view is one block per source document with its flags and missing required fields. The review page is
a self-contained HTML file in <state>/previews/ (0600): inline CSS and images only, no scripts, nothing fetched.
Each value shows a crop of the spot on the page it came from (locate.py), with every detector region blacked out;
cards open when a value looks wrong, a required field is missing, an invoice and receipt for one charge disagree,
or a host's check (config.preview_checks) warns. Receipts and invoices ("money" tasks) also get a vendor, month and
total table to compare with a card or bank statement; a host may add its own wording and sections for them
(config: summary_extras, money_card, money_sections, preview_intro). Only the user opens the page. Stdlib only.
"""
import datetime as dt
import os
import shutil
import subprocess
from collections import Counter

from . import detect, fields as fx, places, tasks
from .config import CFG
from .locate import build_snapshots
from .places import DocError, rel, root, state_dir, write_private
from .scan import Uncapped

NO_AMOUNT = "no amount found — this may be a confirmation, not a receipt"


def _field_name(v):
    return ".".join(x for x in (v["group"], v["plan"],
                                f"row {v['row']}" if v.get("row") else None, v["field"]) if x)


def preview_table(task, out=print):
    """The terminal view, one block per source document, with flags and missing required fields."""
    try:
        prof = fx.load_profile(task["task"])
    except fx.ProfileError:
        prof = {"fields": [], "groups": {}}
    required = [f["name"] for f in prof.get("fields", []) if f.get("required")]
    sources = task.get("sources") or [{"file": v["file"]} for v in task["values"]]
    for n, src in enumerate(sources, 1):
        recs = tasks.typed_records(prof, [v for v in task["values"] if v.get("file") == src["file"]])
        fl = fx.document_flags(recs)
        out(f"[{n}/{len(sources)}] {src['file']}")
        rows = [(_field_name(r), r["value"], f"p.{r['page']} {r['label']}", ("  << " + fl[i]) if i in fl else "")
                for i, r in enumerate(recs)]
        w = [max([len(r[c]) for r in rows] + [1]) for c in range(3)]
        for r in rows:
            out(f"    {r[0].ljust(w[0])}  {r[1].ljust(w[1])}  {r[2].ljust(w[2])}{r[3]}")
        miss = [f for f in required if f not in {r["field"] for r in recs}]
        if miss:
            out(f"    MISSING (required): {', '.join(miss)}")
            if "total" in miss and task["task"] in ("receipt", "invoice"):
                out(f"    {NO_AMOUNT}")
    c = tasks.task_summary(task, prof)
    out(f"{c['documents']} documents, {c['complete']} with every required field, {c['flagged_documents']} flagged")
    if task["missing"]:
        out(f"missing: {', '.join(task['missing'])}")
    if task["dropped"]:
        out(f"dropped as sensitive: {detect.fmt_counts(task['dropped'])}")




# ---------------------------------------------------------------- the review page (preview --html)
MONEY = ("receipt", "invoice")
_CSS = """
:root{color-scheme:light dark;--focus:#0842a0;--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#66665f;--line:#deded8;--red-bg:#fde4e1;--red:#a3221a;
--amber-bg:#fff1cc;--amber:#7a5200;--ok:#1f6b3a;--code:#efefea}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--card:#20201e;--fg:#ececea;--muted:#a3a39b;--line:#3a3a36;
--red-bg:#4a1d1a;--red:#ffb4ab;--amber-bg:#423410;--amber:#ffd77a;--ok:#8fd6a5;--code:#2a2a27;--focus:#8ab4f8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}h1{font-size:1.4rem;margin:.2rem 0 1rem}
.summary{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}.stat{background:var(--card);border:1px solid
var(--line);border-radius:8px;padding:8px 12px}.stat b{display:block;font-size:1.3rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 14px}
.card h2{font-size:1rem;margin:0 0 4px;word-break:break-all}.meta{color:var(--muted);font-size:.9rem;margin:0 0 8px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:5px 8px;
border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:.85rem}
tr.flag td{background:var(--amber-bg)}tr.flag .why{color:var(--amber)}tr.miss td{background:var(--red-bg);
color:var(--red)}.why{font-size:.85rem}.badge{display:inline-block;border-radius:6px;padding:0 6px;font-size:.8rem;
margin-left:6px}.b-red{background:var(--red-bg);color:var(--red)}.b-amber{background:var(--amber-bg);
color:var(--amber)}.b-ok{color:var(--ok)}pre{background:var(--code);padding:10px;border-radius:8px;
white-space:pre-wrap;word-break:break-all;user-select:all}.note{color:var(--muted);font-size:.9rem}
details.card>summary{cursor:pointer;font-weight:600;word-break:break-all}details.card[open]>summary{margin-bottom:6px}
.sub{display:block;font-weight:400;color:var(--muted);font-size:.9rem}.check{color:var(--muted);font-size:.8rem;
font-style:italic}img.snap{display:block;max-width:100%;margin-top:6px;border:1px solid var(--line);border-radius:4px;
background:#fff}.line{margin-top:6px;font:13px/1.4 ui-monospace,monospace;background:var(--code);padding:4px 6px;
border-radius:4px;word-break:break-word}mark{background:#ffd34d;color:#000}.open-why{color:var(--amber)}
a{color:inherit}.sumt td,.sumt th{white-space:nowrap}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}summary,a,pre{min-height:44px}
summary{display:block;padding:10px 0}.meta a{display:inline-block;padding:12px 4px}
.effect{font-weight:600}.counter{font-size:1.1rem;font-weight:700}.alert{background:var(--red-bg);color:var(--red);
border-radius:8px;padding:10px 12px;font-weight:700;margin:8px 0}.tip{color:var(--muted);font-size:.9rem}
th[scope=row]{font-weight:600;color:var(--fg)}.fix{margin-top:6px;color:var(--muted);font-size:.8rem}
.fix code{user-select:all;word-break:break-all;background:var(--code);padding:1px 4px;border-radius:4px}{extra}
.check-warn{background:var(--amber-bg);color:var(--amber);border-radius:6px;padding:6px 8px;margin:0 0 8px}
"""


def preview_css():
    """The page's style sheet, with the host's extra rules (config.preview_css_extra) for its own sections."""
    return _CSS.replace("{extra}", CFG.preview_css_extra)


# Plain words for everything the page shows: field names, flag reasons, matched labels and detector kinds.
PLAIN = {
    "vendor": "vendor", "date": "date", "invoice_date": "invoice date", "invoice_number": "invoice number",
    "period_start": "billing period start", "period_end": "billing period end", "total": "total", "tax": "tax",
    "currency": "currency", "payment_method": "payment method",
    "known sender": "known sender (from the vendor list)", "stripe: from name": "the business name at the top",
    "event heading": "the event's heading", "top of page": "the top of the page", "anywhere": "first one on the page",
    "month in a link": "the month a link on the page names", "file name": "the date in the file name (check it)",
    "dollar sign": "dollar signs on the page", "local model": "the local model (check closely)",
    "long-number": "a long number (hidden)", "account": "an account number (hidden)", "routing": "a routing number "
    "(hidden)", "card": "a card number (hidden)", "ssn": "a social security number (hidden)", "itin": "an ITIN "
    "(hidden)", "ein": "an employer ID (hidden)", "email": "an email address (hidden)", "phone": "a phone number "
    "(hidden)", "address": "a street address (hidden)", "vin": "a vehicle ID (hidden)", "dob": "a birth date (hidden)",
    "member-id": "a member ID (hidden)", "employee-id": "an employee ID (hidden)", "passport": "a passport number "
    "(hidden)", "drivers-license": "a driver's license number (hidden)", "medicare": "a Medicare ID (hidden)",
}


def plain(name):
    """Plain words for an internal name; labels not in PLAIN are the document's own words, shown in quotes."""
    if name in PLAIN:
        return PLAIN[name]
    if name.startswith("table: "):
        return "table column " + plain(name[7:])
    return f'"{name}"' if " " in name or name.isalpha() else name.replace("_", " ")


CHECK = {"vendor": "who billed you?", "date": "invoice or payment date, not a due, renewal or expiry date?",
         "invoice_date": "invoice or payment date, not a due, renewal or expiry date?",
         "total": "the amount actually paid, including tax?", "tax": "matches the tax line?"}


def _amount(v):
    return tasks._amount(v)


def _doc_summary(recs):
    """One document's vendor, date, month and total, plus the host's own keys (config.summary_extras)."""
    v = {r["field"]: r["value"] for r in recs}
    date = v.get("date") or v.get("invoice_date") or ""
    return {"vendor": v.get("vendor") or "", "date": date, "month": date[:7], "total": v.get("total") or "",
            **CFG.summary_extras(v)}


def _near(a, b, days=3):
    try:
        return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days) <= days
    except (TypeError, ValueError):
        return False


def other_documents(task_name, kind):
    """Documents in the other invoice/receipt task files (not superseded): [(task file, source file, summary)]."""
    want = {"invoice": "receipt", "receipt": "invoice"}.get(kind)
    if not want:
        return []
    d, out = state_dir("tasks"), []
    for n in sorted(os.listdir(d)):
        if not n.endswith(".json") or n == task_name:
            continue
        try:
            t = tasks._load_task(os.path.join(d, n))
        except (OSError, ValueError):
            continue
        if t.get("task") != want or t.get("superseded_by"):
            continue
        for src in t.get("sources", []):
            out.append((n, src["file"], _doc_summary([v for v in t["values"] if v.get("file") == src["file"]])))
    return out


def card_plan(task, prof, others=(), name=None):
    """[(source, recs, flags, missing, reasons to open it)] in order; a card opens when flagged or missing a
    required field, when its invoice/receipt pair (same vendor, within 3 days) has another total, or when it is
    the first card of its vendor. The rest start collapsed."""
    required = [f["name"] for f in prof.get("fields", []) if f.get("required")]
    seen, plan = set(), []
    for src in task.get("sources", []):
        recs = tasks.typed_records(prof, [v for v in task["values"] if v.get("file") == src["file"]])
        fl = fx.document_flags(recs)
        miss = [f for f in required if f not in {r["field"] for r in recs}]
        sm = _doc_summary(recs)
        why = []
        if fl:
            why.append(f"{len(fl)} value{'s' if len(fl) > 1 else ''} look{'' if len(fl) > 1 else 's'} wrong")
        if miss:
            why.append("missing " + ", ".join(plain(m) for m in miss))
        for tname, ofile, osm in others:
            if (sm["vendor"] and osm["vendor"].casefold() == sm["vendor"].casefold() and _near(sm["date"], osm["date"])
                    and osm["total"] and sm["total"] and _amount(osm["total"]) != _amount(sm["total"])):
                why.append(f"total differs from its {'receipt' if task['task'] == 'invoice' else 'invoice'} "
                           f"{os.path.basename(ofile)} ({osm['total']})")
        sm["warn"], sm["notes"] = [], []
        if name and task.get("task") in MONEY:  # the host's pair and amount checks (config.preview_checks)
            sm["warn"], sm["notes"] = CFG.preview_checks(task, name, src, {r["field"]: r["value"] for r in recs})
            if sm["warn"]:
                why.append("amount check")
        key = sm["vendor"].casefold()
        if key not in seen:
            seen.add(key)
            if not why:
                why.append("first card of this vendor")
        plan.append((src, recs, fl, miss, why, sm))
    return plan


def _snap_html(sn, e):
    if not sn:
        return ""
    if "png" in sn:
        red = f' title="{sn["redacted"]} sensitive region(s) blacked out"' if sn.get("redacted") else ""
        return f'<img class="snap" alt="where this value is on the page" src="data:image/png;base64,{sn["png"]}"{red}>'
    if "line" in sn:
        b, v, a = sn["line"]
        return f'<div class="line">{e(b)} <mark>{e(v)}</mark> {e(a)}</div>'
    return f'<div class="why">{e(sn.get("note", ""))}</div>'


def wrong_command(name, doc, rec):
    """The command that marks rec, a value of the task's document number doc, wrong (tasks.cmd_wrong). Field names
    are written with dashes (invoice-number), as the page writes no internal names; wrong takes either."""
    import shlex
    field = f'{rec["group"]}.{rec["field"]}'.replace("_", "-")
    cmd = f'{CFG.cli} wrong "{name}" --doc {doc} --field {field}'
    if rec.get("plan"):
        cmd += " --plan " + shlex.quote(rec["plan"])
    if rec.get("row"):
        cmd += f" --row {rec['row']}"
    return cmd


def preview_html(task, name, prof=None, snaps=None, others=()):
    """A self-contained review page: inline CSS and images only, no scripts, nothing fetched. snaps: from
    build_snapshots; others: other_documents, for invoice/receipt pairs."""
    import html
    from urllib.parse import quote
    e = html.escape
    snaps = snaps or {}
    try:
        prof = prof or fx.load_profile(task["task"])
    except fx.ProfileError:
        prof = {"fields": [], "groups": {}}
    c = tasks.task_summary(task, prof)
    state = "superseded by " + task["superseded_by"] if task.get("superseded_by") else (
        "approved" if task.get("approved") is True else "not approved")
    index = {id(v): i for i, v in enumerate(task["values"])}
    plan = card_plan(task, prof, others, name)
    money = task["task"] in MONEY
    cards, opened = [], 0
    for n, (src, recs, fl, miss, why, sm) in enumerate(plan, 1):
        originals = [v for v in task["values"] if v.get("file") == src["file"]]
        rows = []
        for i, r in enumerate(recs):
            sn = snaps.get(index.get(id(originals[i]))) if i < len(originals) else None
            flag = f'<div class="why">looks wrong: {e(fl[i])}</div>' if i in fl else ""
            chk = CHECK.get(r["field"]) or ("invoice or payment date, not a due, renewal or expiry date?"
                                             if r.get("type") == "date" and money else "")
            chk = f'<div class="check">check: {e(chk)}</div>' if chk else ""
            fname_ = " ".join(x for x in (r["plan"], f"row {r['row']}" if r.get("row") else None,
                                          plain(r["field"])) if x)
            fix = f'<div class="fix">wrong? run: <code>{e(wrong_command(name, n, r))}</code></div>'
            rows.append(f'<tr class="{"flag" if i in fl else ""}"><th scope="row">{e(fname_)}{chk}</th>'
                        f'<td><b>{e(r["value"])}</b>{flag}{_snap_html(sn, e)}{fix}</td><td>{e(str(r["page"]))}</td>'
                        f'<td>{e(plain(r["label"]))}</td></tr>')
        for f in miss:
            what = NO_AMOUNT if f == "total" and money else "missing (it is needed)"
            rows.append(f'<tr class="miss"><th scope="row">{e(plain(f))}</th><td>{e(what)}</td><td></td><td></td></tr>')
        sug = src.get("suggest") or {}
        effect = f"If approved: {CFG.assistant} may read these values to answer your questions."
        if money and CFG.money_card:
            dest, fname, effect = CFG.money_card(sm)
        else:
            dest, fname = sug.get("dest") or "ask at file time (--dest)", sug.get("name") or ""
        is_open = bool(why)
        opened += is_open
        warn = sm.get("warn") or []
        checks = "".join(f'<p class="check-warn">{e(w)}</p>' for w in warn) + "".join(
            f'<p class="note">{e(x)}</p>' for x in sm.get("notes") or [])
        badges = (f'<span class="badge b-red">{len(miss)} required missing</span>' if miss else "") + (
            f'<span class="badge b-amber">{len(fl)} flagged</span>' if fl else "") + (
            '<span class="badge b-ok">complete</span>' if not miss and not fl and not warn else "") + (
            f'<span class="badge b-amber">{len(warn)} amount check(s)</span>' if warn else "")
        page1 = min([r["page"] for r in recs if isinstance(r.get("page"), int)] or [1])
        link = "file://" + quote(os.path.join(root(), src["file"])) + f"#page={page1}"
        head = (f'{n}. {e(os.path.basename(src["file"]))}{badges}<span class="sub">{e(sm["vendor"])} '
                f'{e(sm["date"])} {e(sm["total"])}</span>')
        body = (f'<p class="meta">{e(src["file"])} &middot; {e(str(src.get("pages", "?")))} pages &middot; '
                f'<a href="{e(link)}">open original</a><br>goes to: {e(dest)}<br>named: {e(fname)}'
                + (f'<br><span class="open-why">open because: {e("; ".join(why))}</span>' if is_open else "")
                + f'</p>{checks}<div class="wrap"><table><tr><th>field</th><th>value, and where it is</th><th>page</th>'
                f'<th>matched label</th></tr>{"".join(rows)}</table></div>')
        body = f'<p class="effect">{e(effect)}</p>' + body
        label = f"{sm['vendor'] or 'unknown vendor'} {sm['date'] or 'no date'}"
        cards.append(f'<details class="card" role="group" aria-label="{e(label)}"{" open" if is_open else ""}>'
                     f'<summary>{head}</summary>{body}</details>')
    summary = ""
    disagree = sum(1 for *_, why, _sm in plan if any(w.startswith("total differs") for w in why))
    if money:
        rows = sorted((sm["vendor"], sm["month"], sm["total"], os.path.basename(src["file"]))
                      for src, _, _, _, _, sm in plan)
        summary = ('<h2>Compare with your card or bank statement</h2><div class="wrap"><table class="sumt">'
                   '<tr><th>vendor</th><th>month</th><th>total</th><th>document</th></tr>'
                   + "".join(f"<tr><td>{e(v)}</td><td>{e(m)}</td><td>{e(t)}</td><td>{e(f)}</td></tr>"
                             for v, m, t, f in rows) + "</table></div>")
    extra = CFG.money_sections(plan) if money else ""
    stats = [("documents", c["documents"]), ("with every needed field", c["complete"]),
             ("with a value that looks wrong", c["flagged_documents"]), ("fields", c["fields"]),
             ("missing fields", c["missing"]), ("dropped as sensitive", sum(task.get("dropped", {}).values()))]
    approve = f'{CFG.cli} approve "{name}"'
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Review {e(name)}</title><style>{preview_css()}</style></head><body><main>"
            f"<h1>Review: {e(name)}</h1><p class=\"note\">{e(task['task'])} task, created "
            f"{e(task.get('created', '?'))}, {e(state)}. Red: a required field is missing. Amber: a value looks "
            "wrong. Each value shows the spot on the page it came from (highlighted; sensitive numbers blacked "
            f"out).</p><p class=\"effect\">{e(CFG.preview_intro(len(plan)))}</p>"
            f'<p class="tip">Tip: open this page in a private window to keep it out of browser history: '
            f'<code>firefox --private-window '
            f'"{e(os.path.join(root(), CFG.state, "previews", name[:-5] + ".html"))}"</code>'
            "</p>"
            + (f'<div class="alert" role="alert">{disagree} card{"s" if disagree != 1 else ""} disagree'
               f'{"s" if disagree == 1 else ""} with its invoice or receipt pair: check the totals.</div>'
               if disagree else "")
            + f'<p class="counter" role="status">Check these {opened} cards before approving.</p>'
            + "<div class=\"summary\">"
            + "".join(f'<div class="stat"><b>{v}</b>{e(k)}</div>' for k, v in stats) + "</div>" + summary
            + f'<h2 class="todo">Check these {opened} cards</h2><p class="note">The other {len(plan) - opened} '
            "are collapsed: open any to look.</p>" + "".join(cards) + extra
            + "<h2>Approve</h2><p class=\"note\">This page can't approve. When every value is right, run this in "
            f"your own terminal:</p><pre>{e(approve)}</pre><p class=\"note\">When a value is wrong, run the command "
            "under it instead: fields reads that document again without it (taking the next match, or leaving the "
            "field missing) and writes a new task to look at and approve.</p></main></body></html>\n")


def preview_path(task_path):
    d = state_dir("previews")
    os.chmod(d, 0o700)
    return os.path.join(d, os.path.basename(task_path)[:-5] + ".html")


def has_display():
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")) and bool(shutil.which("xdg-open"))


def cmd_preview(args, out=print):
    if not getattr(args, "html", False) and not places.is_tty():
        raise DocError(f"preview is for the user's own terminal; {CFG.assistant} reads only approved task files, "
                       "with read (preview --html writes a page only the user opens)")
    p, kind = tasks.resolve_output(args.target)
    if kind != "task":
        raise DocError("preview shows task files; open an extract in your own editor")
    t = tasks._load_task(p)
    if getattr(args, "html", False):
        hp = preview_path(p)
        uncapped = Uncapped()
        snaps = build_snapshots(t)
        write_private(hp, preview_html(t, os.path.basename(p), snaps=snaps,
                                       others=other_documents(os.path.basename(p), t["task"])))
        c = tasks.task_summary(t)
        plan = card_plan(t, fx.load_profile(t["task"]), other_documents(os.path.basename(p), t["task"]),
                         os.path.basename(p))
        opened = sum(1 for x in plan if x[4])
        why = Counter("first of its vendor" if w == "first card of this vendor" else "pair total differs"
                      if w.startswith("total differs") else "missing a field" if w.startswith("missing")
                      else "amount check" if w == "amount check"
                      else "a value looks wrong" for x in plan for w in x[4])
        kinds = Counter("snapshot" if "png" in v else "line" if "line" in v else "none" for v in snaps.values())
        out(f"wrote {rel(hp)}: {c['documents']} documents, {c['complete']} complete, {c['flagged_documents']} "
            f"flagged; {opened} cards open to check ("
            + (", ".join(f"{k} {n}" for k, n in sorted(why.items())) or "none") + f"), {len(plan) - opened} "
            "collapsed; values shown as "
            + (", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "nothing")
            + f", without a location {len(t['values']) - len(snaps)} (it holds values: only the user opens it)")
        out(f"tip: open it in a private window: firefox --private-window \"{hp}\"")
        if not args.no_open and has_display():
            subprocess.Popen(["xdg-open", hp], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        uncapped.note(out)
        return 0
    if t.get("superseded_by"):
        out(f"note: superseded by {t['superseded_by']}; approve that one instead")
    preview_table(t, out)
    return 0


