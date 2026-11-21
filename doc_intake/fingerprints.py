"""Fingerprints of what the records already hold: sha256 for exact copies, text fingerprints for near copies.

records_index walks the records root (skipping the inbox, the state folder, dot folders, dotfiles and symlinks),
hashes every file in 1 MiB chunks, and fingerprints the first FP_PAGES pages of each document (similar.py: simhash
and MinHash of the words), each file read in a bounded child (reader.run_bounded). The cache,
<state>/records-hashes.json (0600), holds paths, sizes, mtimes, hashes and fingerprints, never text, so later runs
read only new or changed files. Stdlib only.
"""
import json
import os
import tempfile
from collections import Counter

from . import places, reader, similar
from .config import CFG
from .places import inbox, root, state_dir, write_private
from .reader import iter_pages, sniff, _text_pages

RECORDS_HASHES = "records-hashes.json"


def records_hashes_path():
    return os.path.join(state_dir(), RECORDS_HASHES)


def _records_files():
    """(relative path, absolute path) of every file under the records root, except the inbox (with its dot
    folders), the engine's own state folder, dotfiles and dot folders, and symlinks. Nothing is opened here."""
    r = root()
    skip = {os.path.realpath(inbox(create=False)), os.path.realpath(os.path.join(r, CFG.state))}
    for dirpath, dirs, names in os.walk(r):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))
                         and os.path.realpath(os.path.join(dirpath, d)) not in skip)
        for n in sorted(names):
            p = os.path.join(dirpath, n)
            if n.startswith(".") or os.path.islink(p) or not os.path.isfile(p):
                continue
            yield os.path.relpath(p, r), p


FP_PAGES = 5           # text fingerprints cover the first pages only: enough to compare, and bounds the OCR
FP_TYPES = ("pdf", "text") + ("png", "jpeg", "gif", "tiff", "webp", "heic")
FP_TEXT_BYTES = 4 << 20  # a text file's fingerprint reads at most this much
FP_DPI = 200           # fingerprints of files already in the records: enough for word shingles
FP_MAX_BYTES = 512 << 20      # bigger files get a sha256 only, no fingerprint
CACHE_VERSION = 2      # 1: sha256 only; 2: plus the text fingerprint
SAVE_EVERY = 50
SKIPPED = "skipped"    # cache "fingerprint" value prefix: "skipped (too large)", "skipped (timeout)", ...


def page_text_fp(pages):
    """The near-duplicate fingerprint of a document's first FP_PAGES pages ([(n, text)], any iterable, consumed
    one page at a time); no text is kept."""
    f = similar.Fingerprinter()
    for n, t in pages:
        if n <= FP_PAGES:
            f.feed(t)
    return f.result()


def file_fp_info(path, ftype=None):
    """Read the file's first FP_PAGES pages locally (pdftotext, OCR page by page), feeding each page into the
    fingerprint as it is read. {"fp": fingerprint or None, "type", "pages", "method"}: no text."""
    ftype = ftype or sniff(path)
    if ftype not in FP_TYPES:
        return {"fp": None, "type": ftype, "pages": None, "method": "none"}
    doc = {"pages": [], "total": None, "method": "none", "notes": [], "codes": None, "incomplete": None}
    with tempfile.TemporaryDirectory(prefix="docintake-") as tmp:
        os.chmod(tmp, 0o700)
        if ftype == "text":
            gen = _text_pages(path, list(range(1, FP_PAGES + 1)), doc, limit=FP_TEXT_BYTES)
            doc["method"] = "text"
        else:
            gen = iter_pages(path, ftype, doc, tmp, list(range(1, FP_PAGES + 1)), dpi=FP_DPI)
        fp = page_text_fp(gen)
    return {"fp": fp, "type": ftype, "pages": doc["total"], "method": doc["method"]}


def file_text_fp(path):
    """The file's fingerprint, read in-process (file_fp_info); records_index reads each file in a bounded child."""
    return file_fp_info(path)["fp"]


def _mb(maxrss):
    """A ru_maxrss value in MB (reader.maxrss_mb: bytes on macOS, KB on Linux)."""
    return reader.maxrss_mb(maxrss)


def profile_line(relp, size, ftype, pages, method, info, result):
    """One --profile-mem line: names, sizes, types, page counts, memory and whether the memory cap applied
    (mem_cap=yes, no where the OS refused it, - when the file wasn't read in a bounded child), never content."""
    peak = info.get("peak_rss_mb") if info else None
    secs = info.get("seconds") if info else None
    cap = {True: "yes", False: "no"}.get(info.get("capped") if info else None, "-")
    return (f"mem: {relp}  size={size}  type={ftype}  pages={pages if pages is not None else '-'}  "
            f"method={method or '-'}  peak_rss_mb={peak if peak is not None else '-'}  "
            f"secs={secs if secs is not None else '-'}  mem_cap={cap}  "
            f"result={result}")


def _load_records_cache(p):
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        cache = data["files"]
        if not isinstance(cache, dict):
            raise ValueError
        return cache, data.get("version", 1)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None, 0


def _skipped(e):
    return isinstance(e, dict) and str(e.get("fingerprint", "")).startswith(SKIPPED)


def records_index(out=print, retry_skipped=False, profile=None):
    """({sha256: relative path}, [(relative path, text fingerprint)]) of the files already in the records.

    sha256 is over the content bytes, read in chunks. The text fingerprint (similar.py: simhash and MinHash of
    the words) comes from the first FP_PAGES pages, read in a bounded child (run_bounded: CHILD_MEM_MB of address
    space, CHILD_TIMEOUT seconds); the text itself is never stored, printed or logged. Only documents (FP_TYPES, up
    to FP_MAX_BYTES) are read; video, audio, archives and other types get a sha256 only. A file that hits a bound
    is cached as "fingerprint": "skipped (too large)" or "skipped (timeout)" and not tried again unless
    retry_skipped. Cached in <state>/records-hashes.json (0600), keyed by relative path with size and mtime,
    so only new or changed files are read again. The cache holds paths, sizes, mtimes, hashes and fingerprints,
    no text. profile: a function taking one line per file read (profile_line), or None."""
    p = records_hashes_path()
    cache, version = _load_records_cache(p)
    first = cache is None or version < CACHE_VERSION
    cache = cache or {}
    by_sha = {e["sha256"]: (e["text_fp"], e.get("fingerprint")) for e in cache.values()
              if isinstance(e, dict) and e.get("sha256") and "text_fp" in e
              and not (retry_skipped and _skipped(e))}
    fresh, work, told = {}, 0, False
    tally = Counter()

    def save(final):
        files = fresh if final else {**cache, **fresh}
        write_private(p, json.dumps({"version": CACHE_VERSION, "files": files}, indent=1, sort_keys=True))

    try:
        for relp, absp in _records_files():
            try:
                st = os.stat(absp)
            except OSError:
                continue
            old = cache.get(relp)
            same = (isinstance(old, dict) and old.get("size") == st.st_size and old.get("mtime_ns") == st.st_mtime_ns
                    and old.get("sha256"))
            if same and "text_fp" in old and not (retry_skipped and _skipped(old)):
                fresh[relp] = old
                continue
            if first and not told:
                out("fingerprinting existing records (first run)…")
                told = True
            try:
                digest = old["sha256"] if same else places.sha256(absp)
                if digest in by_sha:
                    fp, mark = by_sha[digest]
                    if profile:
                        profile(profile_line(relp, st.st_size, "-", None, None, None, "same bytes as a cached file"))
                else:
                    fp, mark = _fingerprint_record(relp, absp, st.st_size, profile)
            except OSError:
                continue
            by_sha[digest] = (fp, mark)
            entry = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest, "text_fp": fp}
            if mark:
                entry["fingerprint"] = mark
            fresh[relp] = entry
            tally[mark.split(" ")[0] if mark else ("fingerprinted" if fp else "no text")] += 1
            work += 1
            if work % SAVE_EVERY == 0:
                save(final=False)  # an interrupted first run keeps what it has done
                if told:
                    out(f"  {work} files fingerprinted…")
    except KeyboardInterrupt:
        save(final=False)
        raise
    if work or first or set(fresh) != set(cache):
        save(final=True)
    if work:
        out(f"records: {work} read: {tally['fingerprinted']} fingerprinted, {tally['no text']} "
            f"with no text to compare, {tally['sha256']} not documents (sha256 only), {tally[SKIPPED]} skipped")
    skipped = sum(_skipped(e) for e in fresh.values())
    if skipped:
        out(f"records: {skipped} {'file' if skipped == 1 else 'files'} not fingerprinted (too large or timeout); "
            "scan --retry-skipped tries again")
    known, fps = {}, []
    for relp in sorted(fresh):
        known.setdefault(fresh[relp]["sha256"], relp)
        if fresh[relp].get("text_fp"):
            fps.append((relp, fresh[relp]["text_fp"]))
    return known, fps


def _fingerprint_record(relp, absp, size, profile=None):
    """(fingerprint or None, cache mark or None) for one records file. The mark is "sha256 only (<type>)" for a
    file that isn't a document, or "skipped (...)" when the bounded read failed."""
    ftype = sniff(absp)
    if ftype not in FP_TYPES:
        mark = f"sha256 only ({ftype})"
        if profile:
            profile(profile_line(relp, size, ftype, None, None, None, mark))
        return None, mark
    if size > FP_MAX_BYTES:
        mark = "skipped (too large)"
        if profile:
            profile(profile_line(relp, size, ftype, None, None, None, mark))
        return None, mark
    res, info = reader.run_bounded(file_fp_info, absp, ftype)
    if res is None:
        mark = f"{SKIPPED} ({info['reason'] if info['reason'] in ('too large', 'timeout') else 'failed'})"
        if profile:
            profile(profile_line(relp, size, ftype, None, None, info, mark))
        return None, mark
    if profile:
        profile(profile_line(relp, size, ftype, res["pages"], res["method"], info,
                             "fingerprinted" if res["fp"] else "no text"))
    return res["fp"], None


def records_hashes(out=print):
    """{sha256: relative path} of the files already in the records (see records_index)."""
    return records_index(out)[0]
