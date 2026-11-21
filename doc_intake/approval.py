"""Approval of task files and extracts: an integrity hash, and a confirmation only the user can give.

Integrity. `approve` stores `approved_hash`: a sha256 over a canonical serialization of the task's content (its
values, sources with each source file's own sha256, missing, dropped, profile hash: everything except the fields
that change after approval, APPROVAL_KEYS). `read` (and a host's filing step, through tasks.approved_task_for)
recompute it and refuse a task file changed since approval, and refuse when a source document's sha256 no longer
matches the one recorded at `fields` time. Extracts get the same: an `# approved-hash:` header over the extract
minus its mutable header lines. The hash is unkeyed: it catches a task file edited after approval (values changed or fields added),
not a forger who rewrites the file and the hash together; an assistant's tool hook should block its writes there.

Confirmation. `approve` asks on the user's desktop: a zenity (or kdialog) question dialog with the task name, its
document count, totals per group, its flagged count and QUESTION (the host may word it: config.configure).
Cancel is the default button. Only a click on Approve writes the approval. With no display, no dialog tool, or a
cancel, approve refuses, unless --terminal-confirm is given: then the user types back a random four-word phrase,
printed to and read from /dev/tty. That fallback is weaker: a process that drives a pseudo-terminal sees the phrase.
The dialog is a strong speed bump, not a sandbox: a determined local process can still drive a GUI (xdotool,
ydotool, a nested X server). The hook blocks those tools; it cannot see a program written to a file and run later.
"""
import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import subprocess

APPROVAL_KEYS = ("approved", "approved_at", "approved_hash", "keep", "superseded_by", "superseded_at")
# Extract header lines that change after approval and so stay out of its hash.
EXTRACT_MUTABLE = ("approved", "approved-at", "approved-hash", "keep")
QUESTION = "Approve these fields for the assistant to read?"          # set by config.configure(approve_question=)
QUESTION_EXTRACT = "Approve this redacted extract for the assistant to read?"
# Dialog tools only from system directories: a PATH entry the assistant controls must not stand in for zenity.
SYSTEM_BIN = ("/usr/bin", "/bin")
DIALOG_TIMEOUT = 600
WORDS = ("acorn", "anchor", "apple", "arrow", "badge", "basil", "beach", "birch", "blanket", "bottle", "branch",
         "bridge", "bucket", "button", "cabin", "camel", "candle", "canyon", "carpet", "cedar", "chalk", "cherry",
         "chimney", "cloud", "clover", "comet", "copper", "cotton", "crayon", "cricket", "daisy", "desert", "dolphin",
         "dragon", "eagle", "ember", "fabric", "falcon", "feather", "fern", "fiddle", "flint", "forest", "fossil",
         "garden", "ginger", "glacier", "globe", "granite", "gravel", "harbor", "hazel", "helmet", "honey", "island",
         "ivory", "jacket", "jasmine", "kettle", "kite", "ladder", "lantern", "lemon", "lily", "linen", "magnet",
         "maple", "marble", "meadow", "mitten", "mortar", "needle", "nickel", "oasis", "olive", "orbit", "otter",
         "paddle", "pebble", "pepper", "pillow", "pine", "planet", "pocket", "pony", "prairie", "quartz", "quill",
         "rabbit", "radish", "raven", "ribbon", "river", "rocket", "saddle", "salmon", "sandal", "shovel", "silver",
         "spruce", "staple", "stone", "summit", "tablet", "thistle", "thunder", "timber", "tulip", "tunnel", "velvet",
         "violet", "walnut", "willow", "window", "winter", "wren", "yarrow", "zephyr",
         "almond", "beacon", "cactus", "dune", "fjord", "grape", "hammock", "igloo", "juniper", "lagoon")


class ApprovalError(Exception):
    pass


# ---------------------------------------------------------------- integrity
def task_digest(t):
    """sha256 over the task's canonical JSON, without the fields that change after approval."""
    body = {k: v for k, v in t.items() if k not in APPROVAL_KEYS}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def seal_task(t):
    """The approval fields to add to an approved task."""
    return {"approved": True, "approved_at": now(), "approved_hash": task_digest(t)}


def check_task(t, name):
    """Raise ApprovalError unless t is approved and unchanged since."""
    if t.get("approved") is not True:
        raise ApprovalError(f"{name} is not approved: the user previews it, then approves it")
    want = t.get("approved_hash")
    if not want:
        raise ApprovalError(f"{name} was approved without an integrity hash: re-run fields and approve again")
    if want != task_digest(t):
        raise ApprovalError(f"{name} changed since approval; re-run fields and approve again")


def check_sources(sources, locate, digest_of, name):
    """Each source's file still has the sha256 recorded for it. locate(src) -> path or None."""
    for s in sources:
        p = locate(s)
        if not p:
            raise ApprovalError(f"{name}: source document {s.get('file')} is missing; re-run fields and approve again")
        if digest_of(p) != s.get("sha256"):
            raise ApprovalError(f"{name}: source document {s.get('file')} changed since fields ran; re-run fields "
                                "and approve again")


def _split_extract(text):
    head, sep, body = text.partition("\n# ----\n")
    return head.split("\n"), sep, body


def _is_mutable(line):
    return any(line.startswith(f"# {k}:") for k in EXTRACT_MUTABLE)


def extract_digest(text):
    lines, sep, body = _split_extract(text)
    kept = "\n".join(ln for ln in lines if not _is_mutable(ln))
    return hashlib.sha256((kept + sep + body).encode("utf-8")).hexdigest()


def seal_extract(text):
    """The extract text with approved: yes, approved-at and approved-hash set in its header."""
    digest = extract_digest(text)
    lines, sep, body = _split_extract(text)
    lines = [ln for ln in lines if not (ln.startswith("# approved-at:") or ln.startswith("# approved-hash:"))]
    new = [f"# approved-at: {now()}", f"# approved-hash: {digest}"]
    for i, ln in enumerate(lines):
        if ln.startswith("# approved:"):
            lines[i:i + 1] = ["# approved: yes"] + new
            break
    else:
        lines += ["# approved: yes"] + new
    return "\n".join(lines) + sep + body


def check_extract(header, text, name):
    if header.get("approved") != "yes":
        why = " and is marked needs-review" if header.get("needs-review") == "yes" else ""
        raise ApprovalError(f"{name} is not approved{why}: the user looks at it, then approves it")
    want = header.get("approved-hash")
    if not want:
        raise ApprovalError(f"{name} was approved without an integrity hash: extract it again and approve again")
    if want != extract_digest(text):
        raise ApprovalError(f"{name} changed since approval; extract it again and approve again")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- the user's confirmation
def has_display(env=None):
    env = os.environ if env is None else env
    return bool(env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"))


def dialog_tool():
    """(name, absolute path) of zenity or kdialog in a system directory, or None."""
    for name in ("zenity", "kdialog"):
        p = shutil.which(name, path=os.pathsep.join(SYSTEM_BIN))
        if p:
            return name, p
    return None


def dialog_argv(tool, path, title, text):
    if tool == "zenity":
        return [path, "--question", "--title", title, "--text", text, "--no-markup", "--default-cancel",
                "--ok-label", "Approve", "--cancel-label", "Cancel", "--width", "460",
                f"--timeout={DIALOG_TIMEOUT}"]
    return [path, "--title", title, "--yes-label", "Approve", "--no-label", "Cancel", "--yesno", text]


def ask_desktop(title, text, run=subprocess.run):
    """True on a click on Approve, False on cancel, close or timeout, None when no dialog can be shown."""
    tool = dialog_tool()
    if not tool or not has_display():
        return None
    try:
        r = run(dialog_argv(tool[0], tool[1], title, text), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=DIALOG_TIMEOUT + 30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def phrase(n=4):
    return " ".join(secrets.choice(WORDS) for _ in range(n))


def tty_io():
    """(write, readline) on the controlling terminal; ApprovalError when there is none."""
    try:
        f = open("/dev/tty", "r+", encoding="utf-8")
    except OSError as e:
        raise ApprovalError("--terminal-confirm needs a controlling terminal (/dev/tty)") from e

    def write(s):
        f.write(s)
        f.flush()

    return write, f.readline


def ask_terminal(text, io=None):
    """The user types back a random phrase on /dev/tty. True only on an exact match."""
    write, readline = (io or tty_io)()
    want = phrase()
    write(text + "\n\nWEAKER FALLBACK: a program that drives a pseudo-terminal can read this phrase.\n"
          f"To approve, type: {want}\n> ")
    got = " ".join((readline() or "").split()).lower()
    return secrets.compare_digest(got, want)


def confirm(title, text, terminal=False):
    """Raise ApprovalError unless the user confirms. The desktop dialog first; the phrase only with terminal."""
    ans = ask_desktop(title, text)
    if ans is True:
        return "desktop"
    if ans is False:
        raise ApprovalError("not approved: the dialog was cancelled")
    if not terminal:
        raise ApprovalError("not approved: no desktop dialog could be shown (no display, or neither zenity nor "
                            "kdialog in /usr/bin). Run approve in your desktop session, or add --terminal-confirm "
                            "(weaker: you type a phrase)")
    if not ask_terminal(text):
        raise ApprovalError("not approved: the phrase did not match")
    return "terminal"


def summary(name, kind, docs_count, groups, flagged, question=None):
    """The dialog text. groups: [(group, fields or None, total or None)]; question: QUESTION unless given."""
    question = QUESTION if question is None else question
    lines = [f"{kind}: {name}", f"documents: {docs_count}"]
    for g, n, total in groups:
        lines.append(f"  {g}" + (f": {n} fields" if n is not None else "") + (f", total {total}" if total else ""))
    lines += [f"flagged: {flagged}", "", question]
    return "\n".join(lines)
