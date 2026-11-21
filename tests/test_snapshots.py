"""Where each value came from: word boxes, redacted crops, and the review page's summary and collapsing (fake data)."""
import json
import re
import shutil
import subprocess

import host
import pytest
from helpers import ACCOUNT, drop, inbox, run

import doc_intake
from doc_intake import detect, locate, places, preview, reader
from doc_intake import fields as fx
from doc_intake import snapshots as snap
from doc_intake.config import CFG

_need_tools = pytest.mark.skipif(not (shutil.which("pdftotext") and shutil.which("pdftoppm") and reader.magick_cmd()),
                                 reason="poppler-utils and ImageMagick (7 or 6) needed")
_real_have = reader.have


def need_poppler(test):
    """Needs poppler and ImageMagick 7 or 6; runs once per ImageMagick version installed (magick_version)."""
    return _need_tools(pytest.mark.usefixtures("magick_version")(test))


@pytest.fixture(params=["7", "6"])
def magick_version(request, monkeypatch):
    """ImageMagick 7, then forced onto ImageMagick 6's commands (`magick` hidden), each where installed. Where
    `convert` is 7's legacy name (Homebrew) this checks the IM6 argv shapes, not IM6 itself; CI's Ubuntu runs real 6."""
    if request.param == "7" and not _real_have("magick"):
        pytest.skip("ImageMagick 7 not installed")
    if request.param == "6":
        if not (_real_have("convert") and _real_have("identify")):
            pytest.skip("ImageMagick 6 commands not installed")
        monkeypatch.setattr(reader, "have", lambda name: name != "magick" and _real_have(name))
        assert reader.magick_cmd() == ["convert"] and reader.magick_cmd("identify") == ["identify"]
    return request.param


def im(*args):
    """An ImageMagick convert-style command for the test's own checks (7's magick, else 6's convert)."""
    return [*(reader.magick_cmd() or ["magick"]), *args]
IN = host.INBOX


def make_pdf(path, lines):
    """A one-page PDF with a Courier text layer, so pdftotext -layout keeps the columns."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 9 Tf 11 TL 20 760 Td " + " ".join(f"({esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)



@pytest.fixture
def rec(records, monkeypatch):
    monkeypatch.setattr(places, "is_tty", lambda: False)
    monkeypatch.setattr(reader, "_zxing", lambda: None)
    monkeypatch.setattr(preview, "has_display", lambda: False)
    return records


@pytest.fixture
def hooks():
    """Set engine hooks for one test with doc_intake.configure(), and put the old values back after it."""
    saved = {}

    def set_(**kw):
        for k in kw:
            saved.setdefault(k, getattr(CFG, k))
        doc_intake.configure(**kw)
    yield set_
    doc_intake.configure(**saved)
    host.configure()


INVOICE = ["Example Cloud Co", "Invoice number   INV-0001", "Date of issue    March 3, 2030",
           f"Account {ACCOUNT}   Total            $20.00"]


def pixel(png_bytes, x, y, tmp_path):
    p = tmp_path / "px.png"
    p.write_bytes(png_bytes)
    r = subprocess.run(im(str(p), "-format", f"%[fx:int(255*p{{{x},{y}}}.r)] %[fx:int(255*p{{{x},{y}}}.g)] "
                        f"%[fx:int(255*p{{{x},{y}}}.b)]", "info:"), capture_output=True, text=True, check=True)
    return tuple(int(v) for v in r.stdout.split())


def region_max(png_bytes, rect, tmp_path):
    """The brightest channel value (0-1) inside rect of a PNG."""
    p = tmp_path / "rg.png"
    p.write_bytes(png_bytes)
    x0, y0, x1, y1 = (int(v) for v in rect)
    r = subprocess.run(im(str(p), "-alpha", "off", "-crop", f"{x1 - x0}x{y1 - y0}+{x0}+{y0}", "+repage", "-format",
                        "%[fx:maxima]", "info:"), capture_output=True, text=True, check=True)
    return float(r.stdout)


def pdf_doc(tmp_path, lines, name="inv.pdf"):
    p = tmp_path / name
    make_pdf(p, lines)
    return str(p), reader.read_document(str(p), "pdf", barcodes=False)


# ---------------------------------------------------------------- boxes
@need_poppler
def test_bbox_maps_a_value_to_its_words(records, tmp_path):
    p, doc = pdf_doc(tmp_path, INVOICE)
    recs, _, _ = fx.extract(fx.load_profile("invoice"), doc["pages"], "x", keep_span=True)
    where = locate.locate_values(p, "pdf", doc["pages"], recs)
    by = {r["field"]: where.get(i) for i, r in enumerate(recs)}
    total = by["total"]["box"]
    # Courier 9 pt: 5.4 pt per character, lines start at x=20; line 4's baseline is 33 pt below the first
    col = len(f"Account {ACCOUNT}   Total            ")
    assert total[0] == pytest.approx(20 + col * 5.4, abs=2)
    assert total[2] == pytest.approx(20 + (col + 6) * 5.4, abs=2)
    assert 32 + 33 - 10 < total[1] < total[3] < 32 + 33 + 4
    assert by["total"]["unit"] == "pt" and by["total"]["page"] == 1 and by["total"]["page_size"] == [612.0, 792.0]
    assert by["invoice_number"]["box"][0] == pytest.approx(20 + 17 * 5.4, abs=2)


def test_locate_picks_the_same_occurrence():
    text = "Total  $5.00\nTax  $5.00\n"
    words = [("Total", 0, 0, 10, 5), ("$5.00", 20, 0, 30, 5), ("Tax", 0, 10, 10, 15), ("$5.00", 20, 10, 30, 15)]
    second = text.rindex("$5.00")
    assert snap.locate(text, (second, second + 5), words) == (20, 10, 30, 15)
    assert snap.locate(text, (7, 12), words) == (20, 0, 30, 5)


# ---------------------------------------------------------------- crops: highlight and redaction
@need_poppler
def test_crop_has_a_highlight_and_blacks_out_the_account(tmp_path):
    p, doc = pdf_doc(tmp_path, INVOICE)
    text = doc["pages"][0][1]
    words, size = snap.pdf_words(p, 1)
    s = text.index("$20.00")
    box = snap.locate(text, (s, s + 6), words)
    reds = snap.redaction_boxes(text, words)
    a = text.index(ACCOUNT)
    acct = snap.locate(text, (a, a + len(ACCOUNT)), words)
    assert acct in reds  # the account number's box is in the redaction list
    png, region, red_px, hl = snap.snapshot_pdf(p, 1, box, size, reds)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and hl and red_px
    # the highlight: translucent amber over the value (no pure white left at its edge strip)
    cx, cy = int((hl[0] + hl[2]) / 2), int(hl[1]) + 1
    r, g, b = pixel(png, cx, cy, tmp_path)
    assert r > 200 and b < 200 and abs(r - b) > 60
    # the account number: every pixel inside its box is black
    for rect in red_px:
        assert region_max(png, (rect[0] + 1, rect[1] + 1, rect[2] - 1, rect[3] - 1), tmp_path) == 0


@need_poppler
def test_image_crop_blacks_out_and_highlights(tmp_path):
    src = tmp_path / "card.png"
    subprocess.run(im("-size", "800x400", "xc:white", str(src)), check=True, capture_output=True)
    assert snap.image_size(str(src)) == (800, 400)
    box, red = (420, 180, 520, 210), (100, 180, 300, 210)
    png, region, red_px, hl = snap.snapshot_image(str(src), box, (800, 400), [red])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and hl and red_px
    for rect in red_px:
        assert region_max(png, (rect[0] + 1, rect[1] + 1, rect[2] - 1, rect[3] - 1), tmp_path) == 0
    r, g, b = pixel(png, int((hl[0] + hl[2]) / 2), int((hl[1] + hl[3]) / 2), tmp_path)
    assert r > 200 and b < 200


def test_no_imagemagick_means_no_image_snapshot(tmp_path, monkeypatch):
    src = tmp_path / "card.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    monkeypatch.setattr(reader, "have", lambda name: name not in ("magick", "convert", "identify")
                        and _real_have(name))
    monkeypatch.setattr(snap, "_run", lambda cmd, **kw: pytest.fail(f"ran {cmd[0]} without ImageMagick"))
    assert reader.magick_cmd() is None and reader.magick_cmd("identify") is None
    assert snap.snapshot_image(str(src), (420, 180, 520, 210), (800, 400), [(100, 180, 300, 210)]) is None
    assert snap.image_size(str(src)) is None


@pytest.mark.skipif(not (shutil.which("pdftotext") and shutil.which("pdftoppm")), reason="poppler-utils needed")
def test_no_imagemagick_means_no_pdf_snapshot_never_an_unredacted_crop(tmp_path, monkeypatch):
    """pdftoppm renders the crop, but without ImageMagick nothing can black out the account: no PNG at all."""
    p, doc = pdf_doc(tmp_path, INVOICE)
    text = doc["pages"][0][1]
    words, size = snap.pdf_words(p, 1)
    s = text.index("$20.00")
    box = snap.locate(text, (s, s + 6), words)
    reds = snap.redaction_boxes(text, words)
    assert reds
    monkeypatch.setattr(reader, "have", lambda name: name not in ("magick", "convert", "identify")
                        and _real_have(name))
    ran = []
    real_run = snap._run
    monkeypatch.setattr(snap, "_run", lambda cmd, **kw: ran.append(cmd[0]) or real_run(cmd, **kw))
    assert snap.snapshot_pdf(p, 1, box, size, reds) is None
    assert ran == ["pdftoppm"]  # the crop was rendered, then dropped unredacted


def test_redaction_list_covers_detector_spans():
    text = f"Account {ACCOUNT}  Phone: 804-555-0123\nTotal $5.00\n"
    words, x = [], 0
    for m in re.finditer(r"\S+", text):
        words.append((m.group(0), x, 0, x + 10, 5))
        x += 12
    reds = snap.redaction_boxes(text, words)
    flagged = {w[0] for w in words if w[1:] in reds}
    assert ACCOUNT in flagged and "804-555-0123" in flagged and "$5.00" not in flagged


def test_line_context_is_redacted_and_highlights_the_value():
    text = f"Ref {ACCOUNT} Account {ACCOUNT}  Total $5.00  call 804-555-0123\n"
    s = text.index("$5.00")
    before, value, after = snap.line_context(text, (s, s + 5))
    assert value == "$5.00" and ACCOUNT not in before and "555-0123" not in after and "[PHONE]" in after


# ---------------------------------------------------------------- fields and preview end to end
@need_poppler
def test_fields_records_boxes_and_read_hides_them(rec, capsys):
    make_pdf(inbox(rec) / "inv.pdf", INVOICE)
    drop(rec, "r.txt", "Example Shop\nDate: 01/02/2030\nTotal   $7.00\n")
    assert run(["fields", "--task", "invoice", f"{IN}/inv.pdf"]) == 0
    t = sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]
    vals = json.loads(t.read_text())["values"]
    tot = next(v for v in vals if v["field"] == "total")
    assert tot["where"]["box"] and tot["where"]["unit"] == "pt" and tot["where"]["span"]
    assert run(["fields", "--task", "receipt", f"{IN}/r.txt"]) == 0
    t2 = sorted((rec / ".doc-intake" / "tasks").iterdir(), key=lambda p: p.stat().st_mtime)[-1]
    w = next(v for v in json.loads(t2.read_text())["values"] if v["field"] == "total")["where"]
    assert w["span"] and "box" not in w  # text files: a span, no box
    run(["approve", t.name])
    capsys.readouterr()
    run(["read", t.name, "--reason", "test"])
    out = capsys.readouterr().out
    assert '"where"' not in out and '"box"' not in out


@need_poppler
def test_preview_page_snapshots_summary_and_safety(rec, capsys):
    make_pdf(inbox(rec) / "inv.pdf", INVOICE)
    drop(rec, "r.txt", f"Example Shop\nDate: 01/02/2030\nAccount {ACCOUNT}  Total   $7.00\n")
    run(["fields", "--task", "invoice", f"{IN}/inv.pdf", f"{IN}/r.txt"])
    t = sorted((rec / ".doc-intake" / "tasks").iterdir())[-1]
    assert run(["preview", "--html", "--no-open", t.name]) == 0
    html = (rec / ".doc-intake" / "previews" / (t.stem + ".html")).read_text()
    assert "<script" not in html.lower() and "src=\"http" not in html
    rest = re.sub(r'data:image/png;base64,[A-Za-z0-9+/=]+', "", html)
    assert "http" not in rest.lower() and "@import" not in rest and "url(" not in rest
    assert detect.find_spans(rest) == [] and ACCOUNT not in html
    assert html.count('src="data:image/png;base64,') >= 3  # the PDF's values have crops
    assert "<mark>$7.00</mark>" in html  # the text file's value is shown in its (redacted) line
    assert "file://" in html and "inv.pdf#page=1" in html
    assert "check: who billed you?" in html and "check: the amount actually paid, including tax?" in html
    # the summary table: one row per document with its vendor, month and total
    table = html[html.index('class="sumt"'):html.index("</table>", html.index('class="sumt"'))]
    rows = re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", table)
    vals = json.loads(t.read_text())["values"]
    for v, m, tot, f in rows:
        mine = {x["field"]: x["value"] for x in vals if x["file"].endswith(f)}
        assert (v, m, tot) == (mine.get("vendor", ""), (mine.get("invoice_date") or "")[:7], mine.get("total", ""))
    assert len(rows) == 2
    # plain words, no tool names
    for raw in ("long-number", "invoice_number", ">top of page<", ">anywhere<", "known sender<"):
        assert raw not in rest
    assert 'role="status"' in html and 'role="group"' in html and "color-scheme:light dark" in html
    # with no host hooks, the engine's own wording: what approving does, and nothing about filing
    assert "--private-window" in html and "nothing is filed or moved" in html


# ---------------------------------------------------------------- collapsing
def _task(docs_):
    t = {"task": "invoice", "created": "2030-01-01T00:00:00", "sources": [], "values": [], "missing": [],
         "dropped": {}}
    for f, vendor, date, total in docs_:
        t["sources"].append({"file": f"{IN}/{f}", "pages": 1})
        for field, val in (("vendor", vendor), ("invoice_date", date), ("total", total), ("currency", "USD")):
            if val:
                t["values"].append({"group": "invoice", "plan": None, "row": None, "field": field, "value": val,
                                    "file": f"{IN}/{f}", "page": 1, "label": "x"})
    return t


def test_collapsing_rules(records):
    t = _task([("a1.pdf", "Example Cloud Co", "2026-01-05", "$10.00"),
               ("a2.pdf", "Example Cloud Co", "2026-02-05", "$10.00"),     # same vendor again: collapsed
               ("a3.pdf", "Example Cloud Co", "2026-03-05", "$12.00"),     # its receipt says otherwise: open
               ("b1.pdf", "Example Hosting Co", "2026-01-09", "$0.00"),    # flagged (zero total): open
               ("b2.pdf", "Example Hosting Co", "2026-02-09", "$3.00"),    # collapsed
               ("c1.pdf", None, "2026-01-01", "$4.00")])                    # missing vendor: open
    others = [("receipt-x.json", f"{IN}/r3.pdf", {"vendor": "example cloud co", "date": "2026-03-07",
                                                   "total": "$13.00"}),
              ("receipt-x.json", f"{IN}/r2.pdf", {"vendor": "Example Cloud Co", "date": "2026-02-06",
                                                   "total": "$10.00"})]
    prof = fx.load_profile("invoice")
    plan = preview.card_plan(t, prof, others)
    opened = [bool(p[4]) for p in plan]
    assert opened == [True, False, True, True, False, True]
    assert any("differs" in w for w in plan[2][4])
    html = preview.preview_html(t, "invoice-x.json", prof, others=others)
    assert html.count("<details") == 6 and html.count('<details class="card" role="group"') == 6
    assert html.count('" open>') == 4 and "Check these 4 cards before approving" in html
    assert 'role="alert"' in html


def test_vin_detector():
    assert detect.counts(detect.find_spans("VIN 1M8GDM9AXKP042788")) == {"vin": 1}
    assert "vin" not in detect.counts(detect.find_spans("ABCDEFGHJKLMNPRST 1M8GDM9AXKP042789"))


# ---------------------------------------------------------------- host review-page hooks, as the engine calls them
def test_amount_check_opens_the_card(records, hooks):
    t = _task([("a1.pdf", "Example Cloud Co", "2026-01-05", "$10.00"),
               ("a2.pdf", "Example Cloud Co", "2026-02-05", "$900.00")])
    hooks(preview_checks=lambda task, name, src, vals: (
        (["much higher than usual"], []) if vals.get("total") == "$900.00" else ([], ["pairs with its receipt"])))
    prof = fx.load_profile("invoice")
    plan = preview.card_plan(t, prof, (), "invoice-x.json")
    assert plan[1][4] == ["amount check"]
    html = preview.preview_html(t, "invoice-x.json", prof)
    assert '<p class="check-warn">much higher than usual</p>' in html
    assert '<p class="note">pairs with its receipt</p>' in html
    assert "1 amount check(s)" in html and "open because: amount check" in html
    assert html.count('" open>') == 2 and "Check these 2 cards before approving" in html
    assert ".check-warn{" in html and "details.card>summary" in html


def test_money_card_hooks_shape_the_page(records, hooks):
    """summary_extras feeds money_card; money_card, money_sections, preview_intro and preview_css_extra reach the
    page. Without them (the default) the page says only that the assistant may read the values."""
    t = _task([("a.pdf", "Example Cloud Co", "2026-01-05", "$10.00"),
               ("b.pdf", "Example Shop", "2026-02-05", "$1.00")])
    prof = fx.load_profile("invoice")
    plain = preview.preview_html(t, "invoice-x.json", prof)
    assert "If approved: the assistant may read these values to answer your questions." in plain
    assert "ask at file time (--dest)" in plain
    seen = []
    hooks(summary_extras=lambda v: {"shelf": "shelf-" + (v.get("vendor") or "none")[:7]},
          money_card=lambda sm: (seen.append(sm) or (f"Folder/{sm['shelf']}", "card-name.pdf",
                                                     f"If approved: book {sm['total']} on {sm['shelf']}")),
          money_sections=lambda plan: f'<section class="host-extra">{len(plan)} cards planned</section>',
          preview_intro=lambda n: f"Approving {n} documents does a host thing.",
          preview_css_extra=".host-extra{color:red}")
    html = preview.preview_html(t, "invoice-x.json", prof)
    assert [s["shelf"] for s in seen] == ["shelf-Example", "shelf-Example"]
    assert "If approved: book $10.00 on shelf-Example" in html and "goes to: Folder/shelf-Example" in html
    assert "named: card-name.pdf" in html and "the assistant may read these values" not in html
    assert '<section class="host-extra">2 cards planned</section>' in html
    assert "Approving 2 documents does a host thing." in html and ".host-extra{color:red}" in html
    # money hooks are for receipts and invoices only
    other = dict(t, task="paystub")
    html = preview.preview_html(other, "paystub-x.json", fx.load_profile("paystub"))
    assert "host-extra\">" not in html and "If approved: book" not in html
