"""Where a value came from: word boxes for a page, a value's box, and a redacted, highlighted crop of that region.

Boxes come from `pdftotext -bbox` for pages with a text layer and from tesseract's TSV for OCR'd pages and images.
PDF boxes are in PDF points with the origin at the top left of the page (pdftotext's convention); image boxes are
in the image's pixels. A value's box is found by matching the page-text tokens its character span covers against
the word list, by occurrence: the k-th occurrence of those tokens in the text is the k-th in the word list.

Crops are rendered with pdftoppm (PDF) or ImageMagick (images; 7 or 6, reader.magick_cmd), which only ever reads
images here, never a PDF. Every region a sensitive detector flags on the page is blacked out before the crop leaves
this module, so a snapshot never shows more than the value does. Crops and the
words exist only in memory and in the caller's private temp folder; the only output is the PNG for the local
preview page.
"""
import html as htmlmod
import os
import re
import subprocess
import tempfile

from . import detect, reader

RENDER_DPI = 120
PAD_PT = (210, 14, 40, 14)   # left, top, right, bottom padding around a value's box, in points: the label is left
MIN_W_PT = 300
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', re.S)
_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120, **kw)
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- word boxes
def pdf_words(path, n):
    """([(text, x0, y0, x1, y1)], (width, height)) in points for page n of a PDF's text layer, or ([], None)."""
    r = _run(["pdftotext", "-bbox", "-f", str(n), "-l", str(n), path, "-"], text=True, errors="replace")
    if not r or r.returncode != 0:
        return [], None
    m = _PAGE.search(r.stdout)
    if not m:
        return [], None
    words = [(htmlmod.unescape(w[4]), float(w[0]), float(w[1]), float(w[2]), float(w[3]))
             for w in _WORD.findall(m.group(3))]
    return words, (float(m.group(1)), float(m.group(2)))


def ocr_words(image, scale=1.0, psm="4"):
    """[(text, x0, y0, x1, y1)] from tesseract's TSV for an image, each coordinate times scale."""
    r = _run(["tesseract", image, "-", "--psm", psm, "tsv"], text=True, errors="replace")
    if not r or r.returncode != 0:
        return []
    out = []
    for ln in r.stdout.splitlines()[1:]:
        c = ln.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        x, y, w, h = (int(v) for v in c[6:10])
        out.append((c[11].strip(), x * scale, y * scale, (x + w) * scale, (y + h) * scale))
    return out


def tokens(text):
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", text)]


def _seq_positions(seq, toks):
    """Start indexes in toks (a list of strings) where seq matches: inner tokens equal, the first ending with and
    the last starting with seq's ends (a span can cut a token, "$5.00" in "Total:$5.00")."""
    n = len(seq)
    out = []
    for i in range(len(toks) - n + 1):
        if n == 1:
            if seq[0] in toks[i]:
                out.append(i)
            continue
        if toks[i].endswith(seq[0]) and toks[i + n - 1].startswith(seq[-1]) and toks[i + 1:i + n - 1] == seq[1:-1]:
            out.append(i)
    return out


def locate(page_text, span, words):
    """The union box (x0, y0, x1, y1) of the words under page_text[span[0]:span[1]], or None."""
    if not span or not words:
        return None
    s, e = span
    toks = tokens(page_text)
    hit = [i for i, (ts, te, _) in enumerate(toks) if ts < e and s < te]
    if not hit:
        return None
    a, b = hit[0], hit[-1]
    seq = [page_text[max(s, toks[a][0]):min(e, toks[a][1])]] if a == b else (
        [page_text[max(s, toks[a][0]):toks[a][1]]] + [t[2] for t in toks[a + 1:b]] + [page_text[toks[b][0]:min(e, toks[b][1])]])
    seq = [x for x in seq if x]
    if not seq:
        return None
    text_toks = [t[2] for t in toks]
    k = sum(1 for i in _seq_positions(seq, text_toks) if i < a)  # occurrences before this one
    wtoks = [w[0] for w in words]
    found = _seq_positions(seq, wtoks)
    if not found:
        return None
    i = found[min(k, len(found) - 1)]
    ws = words[i:i + len(seq)]
    return (min(w[1] for w in ws), min(w[2] for w in ws), max(w[3] for w in ws), max(w[4] for w in ws))


def redaction_boxes(page_text, words):
    """Boxes to black out: every detector span on the page, plus any word a detector flags on its own."""
    out = []
    for s, e, _kind, _ in detect.find_spans(page_text):
        b = locate(page_text, (s, e), words)
        if b:
            out.append(b)
        # the same digits elsewhere in the word list (a different reading order) are blacked out too
        for w in words:
            if page_text[s:e].strip() and w[0] and len(w[0]) >= 4 and w[0] in page_text[s:e]:
                out.append(w[1:])
    for w in words:
        if detect.find_spans(w[0]):
            out.append(w[1:])
    return sorted(set(out))


# ---------------------------------------------------------------- crops
def crop_box(box, size, unit_per_pt=1.0):
    """The region around a value's box, padded (the label is usually to its left), inside the page."""
    l, t, r, b = (p * unit_per_pt for p in PAD_PT)
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0 - l, y0 - t, x1 + r, y1 + b
    if x1 - x0 < MIN_W_PT * unit_per_pt:
        x1 = x0 + MIN_W_PT * unit_per_pt
    if size:
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(size[0], x1), min(size[1], y1)
    return x0, y0, x1, y1


def _draw(src, dest, rects, highlight):
    """Black rectangles, then a translucent highlight, onto src (ImageMagick). rects and highlight in src pixels."""
    magick = reader.magick_cmd()
    if not magick:
        return False
    cmd = [*magick, *reader.MAGICK_LIMITS, src, "-fill", "black", "-stroke", "none"]
    for x0, y0, x1, y1 in rects:
        cmd += ["-draw", f"rectangle {int(x0) - 1},{int(y0) - 1} {int(x1) + 1},{int(y1) + 1}"]
    if highlight:
        x0, y0, x1, y1 = highlight
        cmd += ["-fill", "rgba(255,196,0,0.35)", "-stroke", "rgb(214,120,0)", "-strokewidth", "2",
                "-draw", f"rectangle {int(x0) - 2},{int(y0) - 2} {int(x1) + 2},{int(y1) + 2}"]
    cmd += ["-strip", dest]
    r = _run(cmd)
    return bool(r and r.returncode == 0 and os.path.exists(dest))


def _inside(rect, region):
    """rect clipped to region and shifted into region's coordinates, or None when they don't meet."""
    x0, y0, x1, y1 = max(rect[0], region[0]), max(rect[1], region[1]), min(rect[2], region[2]), min(rect[3], region[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return x0 - region[0], y0 - region[1], x1 - region[0], y1 - region[1]


def snapshot_pdf(path, n, box, size, redactions, dpi=RENDER_DPI):
    """(png bytes, crop rect in points, [redaction rects in crop pixels], highlight rect in crop pixels) or None."""
    region = crop_box(box, size)
    k = dpi / 72.0
    x, y, w, h = int(region[0] * k), int(region[1] * k), int((region[2] - region[0]) * k), int((region[3] - region[1]) * k)
    if w <= 0 or h <= 0:
        return None
    with tempfile.TemporaryDirectory(prefix="docsnap-") as tmp:
        os.chmod(tmp, 0o700)
        prefix = os.path.join(tmp, "c")
        r = _run(["pdftoppm", "-r", str(dpi), "-f", str(n), "-l", str(n), "-x", str(x), "-y", str(y), "-W", str(w),
                  "-H", str(h), "-png", "-singlefile", path, prefix])
        if not r or r.returncode != 0 or not os.path.exists(prefix + ".png"):
            return None
        px = (x / k, y / k, (x + w) / k, (y + h) / k)
        reds = [tuple(v * k for v in c) for c in (_inside(rb, px) for rb in redactions) if c]
        hl = _inside(box, px)
        hl = tuple(v * k for v in hl) if hl else None
        out = os.path.join(tmp, "out.png")
        if not _draw(prefix + ".png", out, reds, hl):
            return None
        with open(out, "rb") as f:
            return f.read(), region, reds, hl


def snapshot_image(image, box, size, redactions):
    """As snapshot_pdf for an image whose boxes are in its own pixels."""
    region = crop_box(box, size, unit_per_pt=max(1.0, (size[0] if size else 1000) / 612.0))
    x, y, w, h = int(region[0]), int(region[1]), int(region[2] - region[0]), int(region[3] - region[1])
    if w <= 0 or h <= 0:
        return None
    with tempfile.TemporaryDirectory(prefix="docsnap-") as tmp:
        os.chmod(tmp, 0o700)
        crop = os.path.join(tmp, "c.png")
        magick = reader.magick_cmd()
        if not magick:
            return None
        r = _run([*magick, *reader.MAGICK_LIMITS, f"{image}[0]", "-crop", f"{w}x{h}+{x}+{y}", "+repage", crop])
        if not r or r.returncode != 0:
            return None
        reg = (x, y, x + w, y + h)
        reds = [c for c in (_inside(rb, reg) for rb in redactions) if c]
        hl = _inside(box, reg)
        out = os.path.join(tmp, "out.png")
        if not _draw(crop, out, reds, hl):
            return None
        with open(out, "rb") as f:
            return f.read(), region, reds, hl


def image_size(image):
    ident = reader.magick_cmd("identify")
    if not ident:
        return None
    r = _run([*ident, "-ping", "-format", "%w %h\n", f"{image}[0]"], text=True, errors="replace")
    m = re.match(r"(\d+) (\d+)", r.stdout) if r and r.returncode == 0 else None
    return (int(m.group(1)), int(m.group(2))) if m else None


# ---------------------------------------------------------------- text-only sources
def line_context(page_text, span, width=160):
    """(before, value, after) of the line holding span, each redacted on its own; None without a span."""
    if not span:
        return None
    s, e = span
    ls = page_text.rfind("\n", 0, s) + 1
    le = page_text.find("\n", e)
    le = len(page_text) if le == -1 else le
    before, value, after = page_text[ls:s], page_text[s:e], page_text[e:le]
    before, after = before[-width:], after[:width]
    red = lambda t: " ".join(detect.redact(t)[0].split())  # noqa: E731
    return red(before), " ".join(value.split()), red(after)
