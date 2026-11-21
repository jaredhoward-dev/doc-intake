"""A minimal CLI over a config file: `doc-intake [--config PATH] <command>` (installed) or `python3 -m doc_intake ...`
(from a checkout), so the engine runs without writing a host. Help and hints name the form that was run.

The config is a TOML file, found by --config, else $DOC_INTAKE_CONFIG, else ./doc-intake.toml. Its keys:

- root (required): the records root; `~` is expanded, a relative path resolves against the config file's folder;
- inbox, state: folder names under the root (defaults "inbox" and ".doc-intake");
- profiles_dir, senders_file: the field profiles and the known-senders map; relative to the config file's folder;
- model: the local model's name for the optional model pass ("" for none);
- privacy_terms_file: words a model-made title must never contain, one per line; read when needed, never printed;
- [[route]] tables: a simple routing applied to the redacted text, first match wins. Each has category, patterns
  (regular expressions, matched case-insensitively), and optionally min_matches (default 1), profile, dest (a
  folder under the root, with `{year}`) and name (the file name's label). With no rule matching, or no rules, the
  engine's no-route default applies.

Unknown keys are an error, so a typo can't silently keep a default. This CLI adds no way around a guard: approve
still needs the user's own terminal and desktop click, read still needs an approval and a reason, and nothing here
prints document text. Stdlib only.
"""
import argparse
import datetime as dt
import os
import re
import string
import sys
import tomllib

from . import __version__, config as engine_config, fields as fx, llm, pairs, places, preview, reader, scan
from .dates import document_date
from .places import DocError, clean_part, log_access, rel, state_dir
from .reader import EXT
from .scan import NO_PROFILE
from .tasks import (_task_files, cmd_approve, cmd_cleanup, cmd_extract, cmd_fields, cmd_keep, cmd_read,
                    cmd_status)

# The command as the user typed it, for help (argparse's prog) and the hints the engine prints (configure(cli=)).
# main() defaults to the installed console script, which setuptools calls as main() with no arguments;
# __main__.py passes MODULE explicitly. Each entry point says which it is, so nothing guesses from sys.argv[0].
SCRIPT = "doc-intake"
MODULE = "python3 -m doc_intake"
ENV = "DOC_INTAKE_CONFIG"
DEFAULT_NAME = "doc-intake.toml"
KEYS = {"root", "inbox", "state", "profiles_dir", "senders_file", "model", "privacy_terms_file", "route"}
RULE_KEYS = {"category", "patterns", "min_matches", "profile", "dest", "name"}


class ConfigError(DocError):
    pass


# ---------------------------------------------------------------- finding and loading the config
def find_config(flag=None, environ=None, cwd=None):
    """The config file's path: --config, else $DOC_INTAKE_CONFIG, else ./doc-intake.toml. ConfigError when the
    one named doesn't exist, or none is found."""
    environ = os.environ if environ is None else environ
    if flag:
        where = "--config"
        path = flag
    elif environ.get(ENV):
        where = f"${ENV}"
        path = environ[ENV]
    else:
        path = os.path.join(cwd or os.getcwd(), DEFAULT_NAME)
        if not os.path.isfile(path):
            raise ConfigError(f"no config file: pass --config PATH, set ${ENV}, or create ./{DEFAULT_NAME} "
                              "(see examples/doc-intake.toml)")
        return os.path.abspath(path)
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise ConfigError(f"config file named by {where} does not exist: {path}")
    return os.path.abspath(path)


def _str(data, key):
    v = data[key]
    if not isinstance(v, str) or (key != "model" and not v.strip()):
        raise ConfigError(f"{key} must be a non-empty string" if key != "model" else "model must be a string")
    return v


def _path(data, key, base):
    return os.path.normpath(os.path.join(base, os.path.expanduser(_str(data, key))))


def _folder_name(data, key):
    """inbox or state: a relative folder under the root, never climbing out of it."""
    v = _str(data, key)
    if os.path.isabs(v) or ".." in v.replace("\\", "/").split("/"):
        raise ConfigError(f"{key} must be a folder name under root, not {v!r}")
    return v


def _rules(raw):
    if not isinstance(raw, list):
        raise ConfigError("route must be a list of [[route]] tables")
    rules = []
    for i, r in enumerate(raw):
        where = f"route[{i}]"
        if not isinstance(r, dict):
            raise ConfigError(f"{where} must be a table")
        bad = set(r) - RULE_KEYS
        if bad:
            raise ConfigError(f"unknown key in {where}: {', '.join(sorted(bad))}")
        for k in ("category", "patterns"):
            if k not in r:
                raise ConfigError(f"{where}.{k} is required")
        cat = r["category"]
        if not isinstance(cat, str) or not re.fullmatch(r"[a-z0-9_]+", cat):
            raise ConfigError(f"{where}.category must be lower-case letters, digits and _")
        pats = r["patterns"]
        if not isinstance(pats, list) or not pats or not all(isinstance(p, str) and p for p in pats):
            raise ConfigError(f"{where}.patterns must be a non-empty list of strings")
        rxs = []
        for j, p in enumerate(pats):
            try:
                rxs.append(re.compile(p, re.I))
            except re.error as e:
                raise ConfigError(f"{where}.patterns[{j}] is not a valid regular expression: {e}") from None
        need = r.get("min_matches", 1)
        if not isinstance(need, int) or isinstance(need, bool) or not 1 <= need <= len(rxs):
            raise ConfigError(f"{where}.min_matches must be a whole number from 1 to the number of patterns")
        prof = r.get("profile")
        if prof is not None and (not isinstance(prof, str) or not re.fullmatch(r"[a-z0-9_]+", prof)):
            raise ConfigError(f"{where}.profile must be a profile name (lower-case letters, digits and _)")
        dest = r.get("dest")
        if dest is not None:
            if not isinstance(dest, str) or not dest.strip():
                raise ConfigError(f"{where}.dest must be a non-empty string")
            try:
                names = {f for _, f, _, _ in string.Formatter().parse(dest) if f is not None}
            except ValueError as e:
                raise ConfigError(f"{where}.dest is not a valid template: {e}") from None
            if names - {"year"}:
                raise ConfigError(f"{where}.dest may use only {{year}}, not {', '.join(sorted(names - {'year'}))}")
            if os.path.isabs(dest) or ".." in dest.replace("\\", "/").split("/"):
                raise ConfigError(f"{where}.dest must be a folder under root, not {dest!r}")
        label = r.get("name", cat.replace("_", " ").capitalize())
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"{where}.name must be a non-empty string")
        rules.append({"category": cat, "patterns": rxs, "min_matches": need, "profile": prof, "dest": dest,
                      "name": label})
    return rules


def config_source(flag=None, environ=None):
    """How find_config finds the file, as doctor reports it: "--config", "$DOC_INTAKE_CONFIG" or "./doc-intake.toml"."""
    environ = os.environ if environ is None else environ
    if flag:
        return "--config"
    if environ.get(ENV):
        return f"${ENV}"
    return f"./{DEFAULT_NAME}"


def load_config(path):
    """Parse and check the TOML file at path; a dict of plain settings (see the module docstring). ConfigError,
    naming the key, for anything missing, unknown or the wrong type."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    bad = set(data) - KEYS
    if bad:
        raise ConfigError(f"unknown key in {path}: {', '.join(sorted(bad))} (known: {', '.join(sorted(KEYS))})")
    if "root" not in data:
        raise ConfigError(f"root is required in {path}: the records folder")
    base = os.path.dirname(os.path.abspath(path))
    s = {"path": os.path.abspath(path), "root": _path(data, "root", base), "inbox": "inbox", "state": ".doc-intake",
         "profiles_dir": None, "senders_file": None, "model": "", "privacy_terms_file": None, "route": []}
    for k in ("inbox", "state"):
        if k in data:
            s[k] = _folder_name(data, k)
    for k in ("profiles_dir", "senders_file", "privacy_terms_file"):
        if k in data:
            s[k] = _path(data, k, base)
    if s["profiles_dir"] and not os.path.isdir(s["profiles_dir"]):
        raise ConfigError(f"profiles_dir does not exist: {s['profiles_dir']}")
    for k in ("senders_file", "privacy_terms_file"):
        if s[k] and not os.path.isfile(s[k]):
            raise ConfigError(f"{k} does not exist: {s[k]}")
    if "model" in data:
        s["model"] = _str(data, "model").strip()
    if "route" in data:
        s["route"] = _rules(data["route"])
    if s["profiles_dir"]:
        for i, r in enumerate(s["route"]):
            if r["profile"] and not os.path.isfile(os.path.join(s["profiles_dir"], r["profile"] + ".json")):
                raise ConfigError(f"route[{i}].profile names no profile in profiles_dir: {r['profile']}")
    return s


# ---------------------------------------------------------------- routing and the engine's hooks
def classify(rules, text):
    """The first rule with at least min_matches of its patterns found in the (redacted) text, or None."""
    for r in rules:
        if sum(bool(rx.search(text)) for rx in r["patterns"]) >= r["min_matches"]:
            return r
    return None


def make_route(rules):
    """The engine's route hook for these rules: no rule matching falls back to the engine's no-route default."""
    def route(path, ftype, redacted, title_fn=None, **opts):
        rule = classify(rules, redacted)
        if rule is None:
            r = engine_config._no_route(path, ftype, redacted, title_fn, **opts)
            if title_fn and redacted.strip():
                title = title_fn(redacted)
                if title:
                    ext = EXT.get(ftype) or os.path.splitext(path)[1].lower()
                    r.update(name=f"{clean_part(title)}{ext}", name_from_model=True)
            if r["profile"] is None and not r["note"]:
                r["note"] = NO_PROFILE
            return r
        date = document_date(redacted)
        from_file = not date
        if from_file:
            date = dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        ext = EXT.get(ftype) or os.path.splitext(path)[1].lower()
        dest = rule["dest"].format(year=opts.get("year") or date[:4]) if rule["dest"] else None
        note = f"next: fields --task {rule['profile']}" if rule["profile"] else NO_PROFILE
        return {"category": rule["category"], "profile": rule["profile"], "dest": dest,
                "name": f"{date} - {clean_part(rule['name'])}{ext}", "note": note, "date_from_file": from_file,
                "name_from_model": False}
    return route


def make_privacy_terms(path):
    """privacy_terms hook: the file's terms, lower-cased, read on each call; blank and # lines skipped."""
    def terms():
        if not path:
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return [t.strip().lower() for t in f if t.strip() and not t.lstrip().startswith("#")]
        except OSError:
            raise DocError("privacy_terms_file cannot be read") from None
    return terms


def apply(settings, prog=SCRIPT):
    """Reset the engine to its defaults, then configure it from loaded settings; prog is the command the hints
    name."""
    cfg = engine_config.CFG
    for k, v in vars(engine_config.Config()).items():
        setattr(cfg, k, v)
    root = settings["root"]
    name = os.path.basename(settings["path"])
    return engine_config.configure(
        root=lambda: root, inbox=lambda: settings["inbox"], state=settings["state"],
        profiles_dir=settings["profiles_dir"], senders_file=settings["senders_file"],
        route=make_route(settings["route"]), model=lambda: settings["model"],
        privacy_terms=make_privacy_terms(settings["privacy_terms_file"]), cli=prog,
        root_name=f"root (set in {name})", root_missing=f"root does not exist: {root} (set in {name})",
        model_hint=f"set model in {name} or pass --model")


# ---------------------------------------------------------------- commands
def cmd_scan(args, out=print):
    return scan.cmd_scan(args, out, {"year": args.year})


def cmd_pairs(args, out=print):
    """Pairs of approved invoices and receipts, unmatched documents and mismatches (no bookkeeping here)."""
    docs = pairs.documents(_task_files())
    want = {os.path.basename(t) for t in args.tasks}
    keep = (lambda d: d["task"] in want) if want else (lambda d: True)  # noqa: E731
    found, left = pairs.match(docs)
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


# ---------------------------------------------------------------- doctor
TOOLS = ("pdftotext", "pdftoppm", "tesseract")


def _importable(name):
    """True when the optional module imports (zxingcpp, PIL); nothing else is loaded or run."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _probe():
    """The memory-cap probe's child: reads nothing, does nothing."""


def memory_cap():
    """True when the memory cap (RLIMIT_AS) applies on this platform, False when the OS refuses it, None when the
    probe failed: one trivial bounded run that reads no file, with a short timeout."""
    _, info = reader.run_bounded(_probe, timeout=10)
    reader.reset_uncapped()   # the probe is not a read
    return info.get("capped")


def _found(path, kind="dir"):
    ok = os.path.isdir(path) if kind == "dir" else os.path.isfile(path)
    return "ok" if ok else "missing"


def cmd_doctor(args, prog=SCRIPT, out=print):
    """Report the setup without reading any document: Python and doc-intake versions, the config and the places it
    names, the optional tools, whether the memory cap applies, and the local model. Read-only: it creates no folder
    and writes no log. Exit 0 when the config loads and its root exists, 2 otherwise (missing optional tools are
    informational)."""
    v = sys.version_info
    out(f"python: {v.major}.{v.minor}.{v.micro} " + ("ok" if v >= (3, 11) else "too old (3.11 or newer needed)"))
    out(f"doc-intake: {__version__}")
    status, model = 0, None
    try:
        path = find_config(args.config)
        out(f"config: {path} (found by {config_source(args.config)})")
        settings = load_config(path)
        apply(settings, prog)
        out("config loads: ok")
        root = places.root()   # DocError naming the root when it is missing; creates nothing
    except ConfigError as e:
        print(f"doc-intake: config: {e}", file=sys.stderr)
        status = 2
    except DocError as e:   # the root is missing: the same message every other command gives
        out("root: missing")
        print(f"doc-intake: {e}", file=sys.stderr)
        status, model = 2, settings["model"]
    else:
        out("root: ok")
        out(f"inbox: {_found(os.path.join(root, settings['inbox']))}")
        out(f"state folder: {_found(os.path.join(root, settings['state']))}")
        if settings["profiles_dir"]:
            names = fx.profile_names()
            out(f"profiles_dir: ok ({len(names)} profile(s){': ' + ', '.join(names) if names else ''})")
        else:
            out("profiles_dir: not configured")
        senders = settings["senders_file"]
        out(f"senders_file: {_found(senders, 'file') if senders else 'not configured'}")
        out(f"privacy_terms_file: {'configured' if settings['privacy_terms_file'] else 'not configured'}")
        out(f"routing rules: {len(settings['route'])}")
        model = settings["model"]
    for tool in TOOLS:
        out(f"{tool}: {'ok' if reader.have(tool) else 'missing'}")
    magick = reader.magick_cmd()
    out("imagemagick: " + ("missing" if magick is None else "ok (7)" if magick == ["magick"] else "ok (6)"))
    out(f"zxing: {'ok' if _importable('zxingcpp') else 'missing'}")
    out(f"pillow: {'ok' if _importable('PIL') else 'missing'}")
    capped = memory_cap()
    out("memory cap: " + {True: "ok", False: "not available on this platform (the OS refuses it; the timeout and "
                                              "the other limits still apply)"}.get(capped, "unknown (probe failed)"))
    if model is None:
        out("model: unknown (config did not load)")
    elif not model:
        out("model: not configured")
    else:
        out(f"model: {model}")
        if args.no_model:
            out("model server: skipped (--no-model)")
        else:
            out(f"model server: {'ok' if llm.reachable() else 'not reachable on 127.0.0.1:' + str(llm.OLLAMA_PORT)}")
    return status


COMMANDS = {"scan": cmd_scan, "fields": cmd_fields, "preview": preview.cmd_preview, "approve": cmd_approve,
            "read": cmd_read, "keep": cmd_keep, "cleanup": cmd_cleanup, "extract": cmd_extract, "status": cmd_status,
            "pairs": cmd_pairs}


def parser(prog=SCRIPT):
    ap = argparse.ArgumentParser(prog=prog, description="Local-only document intake over a config file. "
                                 f"Config: --config, else ${ENV}, else ./{DEFAULT_NAME}.")
    ap.add_argument("--config", metavar="PATH", help="the TOML config file")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    s = sub.add_parser("scan", help="screen every inbox file: counts, held files, duplicates, routes")
    s.add_argument("--year")
    s.add_argument("--llm-names", action="store_true")
    s.add_argument("--model")
    s.add_argument("--profile-mem", action="store_true")
    s.add_argument("--retry-skipped", action="store_true")
    s = sub.add_parser("fields", help="write a task file of a profile's allowlisted fields")
    s.add_argument("--task", required=True)
    s.add_argument("files", nargs="+", metavar="FILE")
    s.add_argument("--llm", action="store_true")
    s.add_argument("--model")
    s.add_argument("--replaces", metavar="TASK_JSON")
    s.add_argument("--labels", action="store_true")
    s = sub.add_parser("preview", help="the user's preview of a task file (own terminal, or --html)")
    s.add_argument("target", metavar="TASK_JSON")
    s.add_argument("--html", action="store_true")
    s.add_argument("--no-open", action="store_true")
    s = sub.add_parser("pairs", help="invoice and receipt pairs among approved task files")
    s.add_argument("tasks", nargs="*", metavar="TASK_JSON")
    s = sub.add_parser("status", help="task files with counts, never values")
    s.add_argument("--detail", action="store_true")
    s = sub.add_parser("approve", help="the user approves a task file or extract (own terminal, desktop click)")
    s.add_argument("target", metavar="TASK_JSON|EXTRACT")
    s.add_argument("--terminal-confirm", action="store_true")
    s = sub.add_parser("keep", help="keep a task file or extract past cleanup")
    s.add_argument("target", metavar="TASK_JSON|EXTRACT")
    s = sub.add_parser("read", help="read an approved task file or extract, with a reason (logged)")
    s.add_argument("target", metavar="TASK_JSON|EXTRACT")
    s.add_argument("--reason", required=True)
    s.add_argument("--fields")
    s = sub.add_parser("extract", help="a redacted extract of named pages, for the user to approve")
    s.add_argument("files", nargs="+", metavar="FILE")
    s.add_argument("--pages", required=True)
    s.add_argument("--llm-check", action="store_true")
    s.add_argument("--model")
    s = sub.add_parser("doctor", help="check the config, tools, memory cap and model; reads no document")
    s.add_argument("--no-model", action="store_true", help="skip asking the local model server")
    s = sub.add_parser("cleanup", help="delete old task files and extracts")
    s.add_argument("--task")
    s.add_argument("--older-than", type=int, default=30, metavar="DAYS")
    s.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None, prog=SCRIPT):
    """Run one command; the exit status. prog is how the user ran it: SCRIPT (the default, for the console script)
    or MODULE (from __main__.py)."""
    args = parser(prog).parse_args(argv)
    os.umask(0o077)
    if args.cmd == "doctor":
        return cmd_doctor(args, prog)
    try:
        apply(load_config(find_config(args.config)), prog)
    except ConfigError as e:
        print(f"doc-intake: config: {e}", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.cmd](args)
    except (DocError, fx.ProfileError) as e:
        print(f"doc-intake: {e}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nstopped", file=sys.stderr)
        return 130
