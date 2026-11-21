"""Reading documents: type sniffing, text extraction, OCR and barcodes, each file in a bounded child process.

Text comes from pdftotext (PDF text layers), tesseract (scanned pages and images, one page rendered at a time),
the email module (.eml: headers and the plain or HTML body) or the file itself (text, form-feed pages). Barcodes
come from zxingcpp when it and Pillow are installed. Every external tool is optional: without one a file is
reported as unscreened or partly read, never passed as safe.

Bounds: each file is read in a forked child (run_bounded) under RLIMIT_AS (CHILD_MEM_MB, inherited by the tools it
starts; not on macOS, which refuses it) and a wall-clock timeout (CHILD_TIMEOUT), in its own process group so a
timeout kills its tools too; pages and images are scaled to at most MAX_OCR_PIXELS before OCR, and ImageMagick
(7's `magick`, else 6's `convert` and `identify`: magick_cmd) runs under MAGICK_LIMITS. Text never leaves this
module except to the caller, which redacts it before anything is shown. Stdlib only, plus the optional tools.
"""
import os
import pickle
import re
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter

from . import detect
from .places import DocError, rel

MAX_RENDER_PAGES = 30


def have(tool):
    return shutil.which(tool) is not None


def magick_cmd(sub="convert"):
    """The argv prefix for an ImageMagick command, or None when ImageMagick isn't installed. sub is "convert"
    (convert or draw: the prefix takes input, options and output) or "identify".

    ImageMagick 7's `magick` when present (["magick"], ["magick", "identify"]), else ImageMagick 6's own commands
    (["convert"], ["identify"]): Ubuntu 24.04 ships 6. IM6 counts only when both `convert` and `identify` are on
    PATH, since on Windows a lone `convert` is the system's disk converter. MAGICK_LIMITS work on both."""
    if sub not in ("convert", "identify"):
        raise ValueError(sub)
    if have("magick"):
        return ["magick"] if sub == "convert" else ["magick", sub]
    if have("convert") and have("identify"):
        return [sub]
    return None


IMAGE_TYPES = ("png", "jpeg", "gif", "tiff", "webp", "heic")
EXT = {"pdf": ".pdf", "png": ".png", "jpeg": ".jpg", "gif": ".gif", "tiff": ".tif", "webp": ".webp",
       "heic": ".heic", "text": ".txt"}


def sniff(path):
    """The file's type from its first bytes, not its name."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif", b"hevc"):
        return "heic"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head and b"\x00" not in head:
        try:
            head.decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            pass
    return "unknown"


def _zxing():
    try:
        import zxingcpp  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return None
    return zxingcpp


def read_barcodes(images):
    """[(format, text)] of barcodes in the images; None when zxingcpp or PIL is missing. Texts are never printed."""
    zx = _zxing()
    if zx is None:
        return None
    from PIL import Image
    out = []
    for p in images:
        try:
            with Image.open(p) as im:
                out += [(str(r.format), r.text) for r in zx.read_barcodes(im)]
        except Exception:  # an unreadable image is just no barcodes
            continue
    return out


# Memory and time bounds. Each file is read (pdftotext, OCR, image conversion) in a forked child under RLIMIT_AS,
# which its pdftoppm, tesseract and magick children inherit, and a wall-clock timeout; a file that hits either is
# reported, never retried in a loop and never passed as safe. macOS refuses RLIMIT_AS, so there the child runs
# without the memory cap (cap_address_space) and keeps the other bounds.
CHILD_MEM_MB = 1536
CHILD_TIMEOUT = 300    # seconds per file
MAX_OCR_PIXELS = 25_000_000   # pages and images are scaled to at most this many pixels before OCR
OCR_DPI = 300          # inbox screening
BARCODE_DPI = 150
MAX_TEXT_BYTES = 16 << 20     # a text file is read up to this much; past it the read is incomplete
OCR_FAILED = "OCR failed on a page (too large or unreadable)"
MAGICK_LIMITS = ["-limit", "memory", "256MiB", "-limit", "map", "512MiB", "-limit", "disk", "2GiB"]


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def child_mem_mb():
    return _env_int("DOC_INTAKE_CHILD_MEM_MB", CHILD_MEM_MB)


def child_timeout():
    return _env_int("DOC_INTAKE_CHILD_TIMEOUT", CHILD_TIMEOUT)


def cap_address_space(nbytes):
    """Lower this process's address-space limit (RLIMIT_AS) to nbytes; True if it applied, False if the OS refuses.

    Linux applies it, and every tool the process starts inherits it. macOS raises ValueError for RLIMIT_AS, so
    there a bounded read gets no memory cap: it still has the wall-clock timeout, the process-group kill, the
    MemoryError handling, the OCR pixel cap (MAX_OCR_PIXELS) and ImageMagick's own limits (MAGICK_LIMITS). Only
    that ValueError is taken as "unsupported"; any other error propagates."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))
    except ValueError:
        return False
    return True


def maxrss_mb(maxrss):
    """A ru_maxrss value in MB, rounded to 0.1: the kernel reports bytes on macOS and kilobytes on Linux."""
    return round(maxrss / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)


# Bounded reads that ran without the memory cap since the last reset_uncapped(): scan reports the count once a run.
_UNCAPPED = [0]


def reset_uncapped():
    _UNCAPPED[0] = 0


def uncapped_reads():
    """How many bounded reads ran without the memory cap (the OS refused it) since reset_uncapped()."""
    return _UNCAPPED[0]


def _reap_by(pid, deadline):
    """wait4's (pid, status, rusage) once the child has exited, or None if it is still running at deadline."""
    pause = 0.001
    while True:
        r = os.wait4(pid, os.WNOHANG)
        if r[0]:
            return r
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(pause, left))
        pause = min(pause * 2, 0.05)


def run_bounded(fn, *args, mem_mb=None, timeout=None):
    """Run fn(*args) in a forked child limited to mem_mb of address space (RLIMIT_AS, inherited by everything it
    starts) and timeout seconds, in its own process group so a timeout kills its tools too. Where the OS refuses
    RLIMIT_AS (macOS), the child runs without that one cap and keeps the rest (cap_address_space).

    Returns (value, info): info is {"ok", "reason", "peak_rss_mb", "seconds", "capped"}; value is None unless ok.
    reason is "too large" (MemoryError, or killed by a signal), "timeout", or the exception's type name: never text
    from the file. peak_rss_mb is the child's and its tools' peak resident memory (wait4), in MB on every platform.
    capped is True when the memory cap applied, False when the OS refused it (the read ran uncapped; counted for
    uncapped_reads), None when the child died before saying."""
    mem = (mem_mb or child_mem_mb()) << 20
    timeout = timeout or child_timeout()
    rfd, wfd = os.pipe()
    t0 = time.monotonic()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:  # child
        code = 0
        try:
            os.close(rfd)
            os.setsid()
            os.write(wfd, b"1" if cap_address_space(mem) else b"0")  # first byte: did the cap apply
            try:
                payload = ("ok", fn(*args))
            except MemoryError:
                payload, code = ("error", "too large"), 1
            except Exception as e:  # noqa: BLE001 - only the type name goes back
                payload, code = ("error", type(e).__name__), 1
            data = pickle.dumps(payload)
            with os.fdopen(wfd, "wb") as f:
                f.write(data)
        except BaseException:  # noqa: BLE001 - e.g. MemoryError while pickling
            code = 3
        finally:
            os._exit(code)
    os.close(wfd)
    deadline = t0 + timeout
    chunks, timed_out, reaped = [], False, None
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([rfd], [], [], left)
            if ready:
                b = os.read(rfd, 1 << 20)
                if not b:
                    break
                chunks.append(b)
        if not timed_out:
            # EOF comes when the child closes the pipe, a moment before it exits. Killing the group now could
            # catch it in between (a loaded machine deschedules it there) and a finished read would look killed.
            reaped = _reap_by(pid, deadline)
            timed_out = reaped is None
    finally:
        os.close(rfd)
        try:
            # A timeout, or tools the child left behind. After _reap_by the group id stays reserved while any of
            # them is alive (POSIX doesn't reuse it), so this reaches only them; with none left it's a no-op.
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        _, status, ru = reaped or os.wait4(pid, 0)
    data = b"".join(chunks)
    capped = {b"1": True, b"0": False}.get(data[:1])
    if capped is False:
        _UNCAPPED[0] += 1
    info = {"ok": False, "reason": None, "peak_rss_mb": maxrss_mb(ru.ru_maxrss),
            "seconds": round(time.monotonic() - t0, 1), "capped": capped}
    if timed_out:
        info["reason"] = "timeout"
        return None, info
    if os.WIFSIGNALED(status):
        info["reason"] = "too large"  # SIGKILL from the OOM killer, or a crash past the limit
        return None, info
    try:
        kind, value = pickle.loads(data[1:])
    except Exception:  # noqa: BLE001 - the child died before writing
        info["reason"] = "too large" if os.WEXITSTATUS(status) == 3 else "failed"
        return None, info
    if kind != "ok":
        info["reason"] = value
        return None, info
    info["ok"] = True
    return value, info


def _run(cmd, **kw):
    """subprocess.run capturing output, or None when the tool can't be started."""
    try:
        return subprocess.run(cmd, capture_output=True, **kw)
    except OSError:
        return None


def _pdf_info(path, last=MAX_RENDER_PAGES):
    """(page count or None, {page: (width pts, height pts)}) from pdfinfo; nothing of the content."""
    if not have("pdfinfo"):
        return None, {}
    r = _run(["pdfinfo", "-f", "1", "-l", str(last), path], text=True, errors="replace")
    if not r or r.returncode != 0:
        return None, {}
    total, sizes = None, {}
    for line in r.stdout.splitlines():
        m = re.match(r"Pages:\s+(\d+)", line)
        if m:
            total = int(m.group(1))
        m = re.match(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", line)
        if m:
            sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    return total, sizes


def _count_pdf_pages(path):
    """Page objects in a PDF, read in 1 MiB chunks (never the whole file at once)."""
    pat = re.compile(rb"/Type\s*/Page(?![a-z])")
    n, tail = 0, b""
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            buf = tail + chunk
            n += len(pat.findall(buf))
            tail = buf[-32:]
            n -= len(pat.findall(tail))  # counted again with the next chunk
    n += len(pat.findall(tail))
    return n


def page_dpi(size, dpi):
    """The resolution for a page of size (pts) so its render stays within MAX_OCR_PIXELS."""
    if not size:
        return dpi
    w, h = size[0] / 72 * dpi, size[1] / 72 * dpi
    if w * h <= MAX_OCR_PIXELS:
        return dpi
    return max(10, int(dpi * (MAX_OCR_PIXELS / (w * h)) ** 0.5))


def render_page(path, tmp, n, dpi):
    """One page of a PDF as a png path (pdftoppm, else ImageMagick 7), or None. ImageMagick 6 is never asked to
    read a PDF: distribution builds (Ubuntu's) refuse PDF input in their policy.xml, which the engine leaves alone."""
    if have("pdftoppm"):
        prefix = os.path.join(tmp, f"p{dpi}-{n}")
        r = _run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", "-f", str(n), "-l", str(n), path, prefix])
        img = prefix + ".png"
        return img if r and r.returncode == 0 and os.path.exists(img) else None
    if have("magick"):
        img = os.path.join(tmp, f"m{dpi}-{n}.png")
        r = _run([*magick_cmd(), *MAGICK_LIMITS, "-density", str(dpi), f"{path}[{n - 1}]", img])
        return img if r and r.returncode == 0 else None
    return None


def _drop(p):
    try:
        os.remove(p)
    except (OSError, TypeError):
        pass


def ocr(image, notes):
    if not have("tesseract"):
        if "tesseract not installed: no OCR" not in notes:
            notes.append("tesseract not installed: no OCR")
        return ""
    r = _run(["tesseract", image, "-", "--psm", "4"], text=True, errors="replace")
    if not r or r.returncode != 0:
        if OCR_FAILED not in notes:
            notes.append(OCR_FAILED)
        return ""
    return r.stdout


def prepare_image(path, ftype, tmp):
    """The image to OCR: converted to png (heic, webp, gif) and scaled down to MAX_OCR_PIXELS when bigger."""
    conv_cmd, ident = magick_cmd("convert"), magick_cmd("identify")
    if not conv_cmd:
        return path
    big = False
    r = _run([*ident, "-ping", "-format", "%w %h\n", f"{path}[0]"], text=True, errors="replace")
    m = re.match(r"(\d+) (\d+)", r.stdout) if r and r.returncode == 0 else None
    if m:
        big = int(m.group(1)) * int(m.group(2)) > MAX_OCR_PIXELS
    if not big and ftype not in ("heic", "webp", "gif"):
        return path
    conv = os.path.join(tmp, "img.png")
    cmd = [*conv_cmd, *MAGICK_LIMITS, f"{path}[0]"] + (["-resize", f"{MAX_OCR_PIXELS}@>"] if big else []) + [conv]
    r = _run(cmd)
    return conv if r and r.returncode == 0 else path


def _text_pages(path, pages, doc, limit=MAX_TEXT_BYTES):
    """Yield (n, text) for a text file's form-feed pages, read in 1 MiB chunks so only the current page is held.
    Reads at most limit characters; past that doc['incomplete'] is set and the total is unknown."""
    n, buf, left = 1, "", limit
    with open(path, encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(min(1 << 20, left)) if left > 0 else ""
            left -= len(chunk)
            end = not chunk
            if end and left <= 0 and f.read(1):
                doc["incomplete"] = f"text over {limit >> 20} MiB: read in part"
            parts = (buf + chunk).split("\f")
            buf = "" if end else parts.pop()
            for t in parts:
                if not pages or n in pages:
                    yield n, t
                n += 1
            if end:
                doc["total"] = None if doc.get("incomplete") else n - 1
                return


class _HTMLText:
    """HTML to layout-ish text: block tags end lines, table cells are separated by a column gap."""

    def __init__(self):
        from html.parser import HTMLParser
        out = self.out = []

        class P(HTMLParser):
            skip = 0

            def handle_starttag(self, tag, attrs):
                if tag in ("style", "script", "head", "title"):
                    self.skip += 1
                elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"):
                    out.append("\n")
                elif tag in ("td", "th"):
                    out.append("    ")

            def handle_endtag(self, tag):
                if tag in ("style", "script", "head", "title"):
                    self.skip = max(0, self.skip - 1)
                elif tag in ("p", "div", "tr", "table", "h1", "h2", "h3", "h4"):
                    out.append("\n")

            def handle_data(self, data):
                if not self.skip:
                    out.append(" ".join(data.split()) if data.strip() else " ")

        self.parser = P(convert_charrefs=True)

    def text(self, html_src):
        self.parser.feed(html_src)
        self.parser.close()
        lines = ["".join(x).strip() for x in "".join(self.out).split("\n")]
        return "\n".join(ln for ln in lines if ln)


def email_text(path, limit=MAX_TEXT_BYTES):
    """An email file (.eml) as one page: Subject, From and Date, then the plain-text body, or the HTML body as
    text. None when the file is not an email."""
    import email
    from email import policy
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    head = raw[:65536].decode("utf-8", "replace")
    if not re.match(r"[A-Za-z][A-Za-z0-9-]{1,60}:", head) or not re.search(
            r"(?im)^(?:from|subject|mime-version):", head):
        return None
    msg = email.message_from_bytes(raw, policy=policy.default)
    body = None
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        if part is None:
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        body = content if kind == "plain" else _HTMLText().text(content)
        if body and body.strip():
            break
    head_lines = []
    if msg.get("Subject"):
        head_lines.append("Subject: " + " ".join(str(msg["Subject"]).split()))
    try:
        sender = msg["From"].addresses[0].display_name if msg.get("From") else ""
    except (AttributeError, IndexError, TypeError):
        sender = ""
    if sender:
        head_lines.append("From: " + " ".join(sender.split()))
    try:
        sent = msg["Date"].datetime if msg.get("Date") else None
    except (AttributeError, TypeError, ValueError):
        sent = None
    if sent:
        head_lines.append("Date: " + sent.date().isoformat())
    return "\n".join(head_lines) + "\n\n" + (body or "")


def iter_pages(path, ftype, doc, tmp, pages=None, dpi=OCR_DPI, barcodes=False):
    """Yield (n, text) one page at a time, filling doc's total, method, notes and codes. Each page's image is
    rendered alone, read, and deleted before the next. Text never leaves this process except through redaction."""
    if ftype == "text":
        mail = email_text(path)
        if mail is not None:
            doc["method"], doc["total"] = "email", 1
            if not pages or 1 in pages:
                yield 1, mail
            return
        doc["method"] = "text"
        yield from _text_pages(path, pages, doc)
    elif ftype == "pdf":
        total, sizes = _pdf_info(path)
        first, last = (min(pages), max(pages)) if pages else (1, None)
        if total is not None and last is not None:
            last = min(last, total)
        texts = {}
        if have("pdftotext"):
            cmd = ["pdftotext", "-layout"] + (["-f", str(first), "-l", str(last)] if last else []) + [path, "-"]
            r = _run(cmd, text=True, errors="replace")
            if r and r.returncode == 0:
                parts = r.stdout.split("\f")
                del r
                if parts and not parts[-1].strip():
                    parts = parts[:-1]
                texts = {first + i: t for i, t in enumerate(parts)}
                doc["method"] = "pdftotext"
        else:
            doc["notes"].append("pdftotext not installed")
        if total is None:
            total = (max(texts) if texts and not pages else None) or max(_count_pdf_pages(path), 1)
        doc["total"] = total
        want = [n for n in range(1, total + 1) if not pages or n in pages]
        no_ocr = not have("tesseract")
        ocred, rendered, plain = False, 0, 0
        if no_ocr and any(len(re.sub(r"\s", "", texts.get(n, ""))) < 20 for n in want):
            doc["notes"].append("tesseract not installed: no OCR")
        codes_on = barcodes and _zxing() is not None
        for n in want:
            t = texts.pop(n, "")
            img = None
            if len(re.sub(r"\s", "", t)) < 20 and not no_ocr and rendered < MAX_RENDER_PAGES:
                rendered += 1
                img = render_page(path, tmp, n, page_dpi(sizes.get(n), dpi))
                if img:
                    t = ocr(img, doc["notes"])
                    ocred = ocred or bool(t.strip())
                elif OCR_FAILED not in doc["notes"]:
                    doc["notes"].append(OCR_FAILED)
            if codes_on and (img or plain < 10):
                if not img:
                    plain += 1
                    img = render_page(path, tmp, n, page_dpi(sizes.get(n), BARCODE_DPI))
                if img:
                    doc["codes"] += read_barcodes([img])
            _drop(img)
            yield n, t
        if ocred:
            doc["method"] = "ocr" if doc["method"] == "none" else doc["method"] + "+ocr"
        if barcodes and not codes_on:
            doc["codes"] = None
    elif ftype in IMAGE_TYPES:
        doc["total"] = 1
        img = prepare_image(path, ftype, tmp)
        text = ocr(img, doc["notes"])
        if text.strip():
            doc["method"] = "ocr"
        if barcodes:
            doc["codes"] = read_barcodes([img])
        if not pages or 1 in pages:
            yield 1, text
    else:
        doc["notes"].append(f"unsupported type {ftype}: not read")
    if OCR_FAILED in doc["notes"]:
        doc["incomplete"] = OCR_FAILED


def read_document(path, ftype=None, pages=None, barcodes=True, dpi=OCR_DPI):
    """{'pages': [(n, text)], 'total': n, 'method': ..., 'notes': [...], 'codes': [(format, text)] or None,
    'incomplete': reason or None}. pages limits which pages are read (1-based). Text never leaves this process
    except through redaction. In-process: callers that read untrusted sizes use read_document_bounded."""
    ftype = ftype or sniff(path)
    doc = {"pages": [], "total": None, "method": "none", "notes": [], "codes": [] if barcodes else None,
           "incomplete": None}
    with tempfile.TemporaryDirectory(prefix="docintake-") as tmp:
        os.chmod(tmp, 0o700)
        doc["pages"] = list(iter_pages(path, ftype, doc, tmp, pages, dpi, barcodes))
    if barcodes and doc["codes"] is None:
        doc["notes"].append("zxingcpp not available: no QR or barcode check")
    return doc


def read_document_bounded(path, ftype=None, pages=None, barcodes=True):
    """read_document in a bounded child (run_bounded). (doc, info); on failure doc is empty with
    doc['unread'] = 'too large' or 'timeout' and a note."""
    doc, info = run_bounded(read_document, path, ftype, pages, barcodes)
    if doc is None:
        why = info["reason"] if info["reason"] in ("too large", "timeout") else "failed"
        note = {"too large": f"not read: too large (over {child_mem_mb()} MB)",
                "timeout": f"not read: timeout (over {child_timeout()} s)"}.get(why, "not read: the reader failed")
        doc = {"pages": [], "total": None, "method": "none", "codes": None, "incomplete": why, "unread": why,
               "notes": [note]}
    return doc, info


def barcode_counts(codes):
    c = Counter()
    for fmt, text in codes or ():
        t = (text or "").strip()
        if t.lower().startswith(("otpauth://", "otpauth-migration://")):
            c["2fa"] += 1
        elif "PDF417" in fmt.upper() and (t.startswith("@") or "ANSI " in t[:40]):
            c["drivers-license"] += 1
        else:
            c["barcode"] += 1
    return dict(c)


def sensitive_counts(doc):
    c = Counter()
    for _, text in doc["pages"]:
        c.update(detect.counts(detect.find_spans(text)))
    c.update(barcode_counts(doc["codes"]))
    return dict(c)


def read_bounded_or_fail(p, pages=None, barcodes=True, whole=False):
    """read_document_bounded, raising DocError when the file couldn't be read (or, with whole, read fully)."""
    doc, _ = read_document_bounded(p, None, pages, barcodes)
    if doc.get("unread") or (whole and doc.get("incomplete")):
        raise DocError(f"{rel(p)}: {'; '.join(doc['notes'][-1:]) or 'not fully read'}: not screened")
    return doc
