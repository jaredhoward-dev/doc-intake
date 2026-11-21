"""Where each value came from: its span on the page, its box from the PDF text layer or OCR, and the redacted
snapshots the review page shows (snapshots.py). Every source is read again in a bounded child; nothing of the
text leaves except the crops, with every detector region blacked out. Stdlib only, plus the optional tools.
"""
import os
import tempfile

from . import places, reader, snapshots as snap
from .places import root
from .reader import IMAGE_TYPES, OCR_DPI, _drop, _pdf_info, page_dpi, prepare_image, render_page, sniff


def _value_span(rec, pages):
    """rec's span on its page, or the first case-insensitive occurrence of its value there (known senders)."""
    text = dict(pages).get(rec["page"], "")
    if rec.get("span"):
        return rec["span"]
    v = rec.get("value") or ""
    if len(v) >= 3:
        i = text.lower().find(v.lower())
        if i >= 0:
            return [i, i + len(v)]
    return None


def page_words(path, ftype, n, page_text, tmp):
    """(words, page size, unit) for page n: the PDF text layer when it matches the page text, else OCR boxes
    (scaled to points for a PDF page, pixels for an image)."""
    if ftype == "pdf":
        words, size = snap.pdf_words(path, n)
        toks = {t[2] for t in snap.tokens(page_text)}
        if words and sum(w[0] in toks for w in words) >= max(1, len(words) // 3):
            return words, size, "pt"
        if not reader.have("tesseract"):
            return [], size, "pt"
        sizes = _pdf_info(path)[1] or {}
        dpi = page_dpi(sizes.get(n), OCR_DPI)
        img = render_page(path, tmp, n, dpi)
        if not img:
            return [], size, "pt"
        words = snap.ocr_words(img, 72.0 / dpi)
        _drop(img)
        return words, size or sizes.get(n), "pt"
    if ftype in IMAGE_TYPES and reader.have("tesseract"):
        img = prepare_image(path, ftype, tmp)
        return snap.ocr_words(img), snap.image_size(img), "px"
    return [], None, None


def locate_values(path, ftype, pages, records):
    """{record index: {"page", "span", and when boxes exist "box" [x0, y0, x1, y1], "unit", "page_size"}}."""
    out, cache = {}, {}
    with tempfile.TemporaryDirectory(prefix="docintake-") as tmp:
        os.chmod(tmp, 0o700)
        for i, r in enumerate(records):
            span = _value_span(r, pages)
            if not span:
                continue
            w = {"page": r["page"], "span": span}
            if ftype in ("pdf",) + IMAGE_TYPES:
                if r["page"] not in cache:
                    cache[r["page"]] = page_words(path, ftype, r["page"], dict(pages).get(r["page"], ""), tmp)
                words, size, unit = cache[r["page"]]
                box = snap.locate(dict(pages).get(r["page"], ""), span, words)
                if box:
                    w.update(box=[round(v, 1) for v in box], unit=unit,
                             page_size=[round(v, 1) for v in size] if size else None)
            out[i] = w
    return out


def build_snapshots(task):
    """{value index in task["values"]: {"png": base64} or {"line": (before, value, after)} or {"note": why}},
    with every detector region on the page blacked out of each crop. Reads each source again, in a child."""
    import base64
    out = {}
    for src in task.get("sources", []):
        idx = [i for i, v in enumerate(task["values"]) if v.get("file") == src["file"]]
        p = os.path.join(root(), src["file"])
        if not idx:
            continue
        if not os.path.isfile(p) or (src.get("sha256") and places.sha256(p) != src["sha256"]):
            for i in idx:
                out[i] = {"note": "the original is no longer where it was (or changed): no snapshot"}
            continue
        vals = [task["values"][i] for i in idx]
        got, _ = reader.run_bounded(_snapshots_for, p, vals)
        for k, v in (got or {}).items():
            if "png" in v:
                v = {"png": base64.b64encode(v["png"]).decode("ascii"), "redacted": v["redacted"]}
            out[idx[k]] = v
    return out


def _snapshots_for(p, vals):
    ftype = sniff(p)
    doc = reader.read_document(p, ftype, barcodes=False)
    pages = dict(doc["pages"])
    out, cache = {}, {}
    with tempfile.TemporaryDirectory(prefix="docintake-") as tmp:
        os.chmod(tmp, 0o700)
        for k, v in enumerate(vals):
            w = v.get("where") or {}
            text = pages.get(w.get("page") or v.get("page"), "")
            if w.get("box") and ftype in ("pdf",) + IMAGE_TYPES:
                n = w["page"]
                if n not in cache:
                    words, size, unit = page_words(p, ftype, n, text, tmp)
                    cache[n] = (snap.redaction_boxes(text, words), size,
                                prepare_image(p, ftype, tmp) if ftype in IMAGE_TYPES else None)
                reds, size, img = cache[n]
                size = size or (tuple(w["page_size"]) if w.get("page_size") else None)
                shot = (snap.snapshot_image(img, tuple(w["box"]), size, reds) if img else
                        snap.snapshot_pdf(p, n, tuple(w["box"]), size, reds))
                if shot:
                    out[k] = {"png": shot[0], "redacted": len(shot[2])}
                    continue
            ctx = snap.line_context(text, w.get("span"))
            out[k] = {"line": ctx} if ctx else {"note": "no location recorded for this value"}
    return out


