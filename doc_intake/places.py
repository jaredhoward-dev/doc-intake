"""Places and private files: the records root, the inbox, the state folder, the index, the read log.

Everything the engine writes is private: folders 0700, files 0600, written atomically (a temp file, then rename).
The access log is append-only by convention: one tab-separated line per approve, read, keep, cleanup, extract,
supersede or pairs run, with a timestamp, the target and a reason, never a value. Stdlib only.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys

from .config import CFG


class DocError(Exception):
    pass


# ---------------------------------------------------------------- places
def root():
    if CFG.root is None:
        raise DocError("the engine is not configured: no records root")
    r = CFG.root()
    if not os.path.isdir(r):
        raise DocError(CFG.root_missing)
    return os.path.realpath(r)


def inbox(create=True):
    p = os.path.join(root(), CFG.inbox())
    if create and not os.path.isdir(p):
        os.makedirs(p, mode=0o700, exist_ok=True)
        os.chmod(p, 0o700)
    return p


def state_dir(*parts):
    p = os.path.join(root(), CFG.state, *parts)
    os.makedirs(p, mode=0o700, exist_ok=True)
    return p


def rel(path):
    return os.path.relpath(os.path.realpath(path), root())


def inside(path, base):
    path, base = os.path.realpath(path), os.path.realpath(base)
    return os.path.commonpath([path, base]) == base


def write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def log_access(action, target, what, reason=""):
    p = os.path.join(state_dir(), "access.log")
    clean = lambda s: " ".join(str(s).split())  # noqa: E731 - one line, no tabs
    line = "\t".join([dt.datetime.now().isoformat(timespec="seconds"), action, clean(target), clean(what),
                      "reason=" + clean(reason)]) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line)
    os.chmod(p, 0o600)


def is_tty():
    return sys.stdout.isatty()


# ---------------------------------------------------------------- index
def index_path():
    return os.path.join(state_dir(), "index.json")


def load_index():
    try:
        with open(index_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 1, "files": {}}


def save_index(idx):
    write_private(index_path(), json.dumps(idx, indent=1, sort_keys=True))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- names and moves
def clean_part(s):
    s = "".join(ch for ch in str(s) if ch.isprintable())
    s = re.sub(r"[/\\:*?\"<>|]", "-", s)
    return " ".join(s.split()).strip(" .-")[:80].strip(" .-")


def unique(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def move_no_overwrite(src, dest):
    """Move src to dest, never replacing a file: a hard link fails if dest exists; fall back to copy + remove."""
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    while True:
        try:
            os.link(src, dest)
            break
        except FileExistsError:
            dest = unique(dest)
        except OSError:
            dest = unique(dest)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
            break
    os.chmod(dest, 0o600)
    os.unlink(src)
    return dest
