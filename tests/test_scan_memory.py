"""Bounded memory in the engine: the per-file child limit, streaming reads, page-by-page OCR and --profile-mem.

Through the fake host (tests/host.py) against a throwaway records root; every document here is made up. The
bounded child's memory cap is RLIMIT_AS, which only Linux lets a process lower, so the tests that assert the cap
bites are skipped elsewhere (CI runs them on Ubuntu); the rest run the real fork everywhere, and on macOS the
engine itself runs the child without that one cap (reader.cap_address_space)."""
import hashlib
import json
import os
import sys
import time

import pytest

import host
from doc_intake import fingerprints, places, reader, similar
from helpers import rec  # noqa: F401  (a fixture)

WORDS = ("garden shed inventory rake hoe trowel shovel bucket hose nozzle gloves twine stakes netting compost "
         "mulch seeds labels pots trays").split()
TEXT = " ".join(WORDS * 6)
SENTINEL = "zebrafinch quartzlamp"

cap_bites = pytest.mark.skipif(not sys.platform.startswith("linux"),
                               reason="asserts the memory cap stops a read: RLIMIT_AS, which only Linux lets a "
                                      "process lower (macOS refuses it and runs uncapped)")


def put(root, relp, data):
    p = root / relp
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)
    return p


def cache(root):
    return json.loads((root / host.STATE / fingerprints.RECORDS_HASHES).read_text())["files"]


def hog(*_a, **_k):
    """A fake extractor that allocates far past the child's limit."""
    return bytearray(2 << 30)



# ---------------------------------------------------------------- the bounded child
def test_run_bounded_returns_the_value():
    value, info = reader.run_bounded(lambda a, b: a + b, 2, 3, mem_mb=512, timeout=30)
    assert value == 5 and info["ok"] and info["reason"] is None and info["peak_rss_mb"] > 0


def refuse_rlimit_as(monkeypatch, exc=ValueError):
    """setrlimit as on macOS: RLIMIT_AS raises (ValueError by default), every other limit is real."""
    real = reader.resource.setrlimit

    def setrlimit(which, limits):
        if which == reader.resource.RLIMIT_AS:
            raise exc("current limit exceeds maximum limit")
        return real(which, limits)
    monkeypatch.setattr(reader.resource, "setrlimit", setrlimit)


def test_cap_address_space_reports_a_refused_cap(monkeypatch):
    refuse_rlimit_as(monkeypatch)
    assert reader.cap_address_space(256 << 20) is False


def test_cap_address_space_does_not_swallow_other_errors(monkeypatch):
    refuse_rlimit_as(monkeypatch, exc=OSError)
    with pytest.raises(OSError):
        reader.cap_address_space(256 << 20)


def test_run_bounded_reads_without_the_cap_where_refused(monkeypatch):
    refuse_rlimit_as(monkeypatch)
    value, info = reader.run_bounded(lambda a, b: a * b, 6, 7, mem_mb=256, timeout=30)
    assert value == 42 and info["ok"] and info["reason"] is None
    value, info = reader.run_bounded(time.sleep, 30, mem_mb=256, timeout=1)  # the timeout still holds
    assert value is None and info["reason"] == "timeout"


@pytest.mark.parametrize("platform, maxrss", [("darwin", 300 << 20), ("linux", 300 << 10)])
def test_maxrss_is_converted_per_platform(monkeypatch, platform, maxrss):
    monkeypatch.setattr(sys, "platform", platform)
    assert reader.maxrss_mb(maxrss) == 300.0 == fingerprints._mb(maxrss)
    assert reader.maxrss_mb(maxrss + maxrss // 600) == 300.5


def test_run_bounded_peak_rss_is_plausible_megabytes():
    _, info = reader.run_bounded(lambda: len(bytearray(64 << 20)), mem_mb=1024, timeout=30)
    assert 64 <= info["peak_rss_mb"] < 1024


@cap_bites
def test_run_bounded_too_large_does_not_crash():
    value, info = reader.run_bounded(hog, mem_mb=256, timeout=30)
    assert value is None and not info["ok"] and info["reason"] == "too large"


def test_run_bounded_timeout_kills_the_child():
    t0 = time.monotonic()
    value, info = reader.run_bounded(time.sleep, 30, mem_mb=256, timeout=1)
    assert value is None and info["reason"] == "timeout" and time.monotonic() - t0 < 10


def test_run_bounded_reports_only_the_exception_type():
    def boom():
        raise ValueError("secret page text")
    value, info = reader.run_bounded(boom, mem_mb=256, timeout=30)
    assert value is None and info["reason"] == "ValueError"


@cap_bites
def test_records_file_too_large_is_marked_skipped_not_retried(rec, monkeypatch, tmp_path, capsys):
    put(rec, "Archive/big.txt", TEXT)
    monkeypatch.setenv("DOC_INTAKE_CHILD_MEM_MB", "256")
    marker = tmp_path / "calls"

    def counted_hog(path, ftype=None):
        with open(marker, "a") as f:  # the child can't report back through a closure, so count in a file
            f.write("x")
        return hog()

    monkeypatch.setattr(fingerprints, "file_fp_info", counted_hog)
    known, fps = fingerprints.records_index()
    entry = cache(rec)["Archive/big.txt"]
    assert entry["fingerprint"] == "skipped (too large)" and entry["text_fp"] is None and entry["sha256"]
    assert fps == [] and "Archive/big.txt" in known.values()
    assert "1 file not fingerprinted" in capsys.readouterr().out
    fingerprints.records_index()
    assert marker.read_text() == "x"  # cached as skipped: not read again
    fingerprints.records_index(retry_skipped=True)
    assert marker.read_text() == "xx"


def test_records_skips_non_documents_without_reading(rec, monkeypatch):
    put(rec, "Media/clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4096)
    put(rec, "Media/photos.zip", b"PK\x03\x04" + b"\x00" * 100)
    real = reader.run_bounded
    monkeypatch.setattr(reader, "run_bounded", lambda fn, path, *a, **k: pytest.fail("a non-document was read")
                        if "Media" in path else real(fn, path, *a, **k))
    fingerprints.records_index()
    c = cache(rec)
    assert c["Media/clip.mp4"]["fingerprint"] == "sha256 only (unknown)"
    assert c["Media/photos.zip"]["fingerprint"] == "sha256 only (zip)"
    assert c["Media/clip.mp4"]["sha256"] == hashlib.sha256((rec / "Media/clip.mp4").read_bytes()).hexdigest()


@cap_bites
def test_inbox_file_too_large_is_unscreened(rec, monkeypatch, capsys):
    monkeypatch.setenv("DOC_INTAKE_CHILD_MEM_MB", "256")
    put(rec, "inbox/huge.txt", TEXT)
    monkeypatch.setattr(reader, "read_document", hog)
    assert host.main(["scan"]) == 0
    out = capsys.readouterr().out
    assert "UNSCREENED (too large)" in out and "not read: too large (over 256 MB)" in out
    assert "1 file not fully read" in out and "none" not in out.split("huge.txt")[1].split("\n")[0]


@cap_bites
def test_extract_refuses_a_file_too_large(rec, monkeypatch, capsys):
    monkeypatch.setenv("DOC_INTAKE_CHILD_MEM_MB", "256")
    put(rec, "inbox/huge.txt", TEXT)
    monkeypatch.setattr(reader, "read_document", hog)
    assert host.main(["extract", "inbox/huge.txt", "--pages", "1"]) == 2
    assert "not screened" in capsys.readouterr().err


# ---------------------------------------------------------------- streaming reads
def test_sha256_reads_in_chunks(tmp_path, monkeypatch):
    p = tmp_path / "blob.bin"
    data = os.urandom(3 * (1 << 20) + 17)
    p.write_bytes(data)
    sizes = []
    real_open = open

    class Spy:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            sizes.append(n)
            return self.f.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

    monkeypatch.setattr(places, "open", lambda *a, **k: Spy(real_open(*a, **k)), raising=False)
    assert places.sha256(str(p)) == hashlib.sha256(data).hexdigest()
    assert sizes and all(0 < n <= 1 << 20 for n in sizes) and len(sizes) >= 4


def test_text_pages_stream_and_cap(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("one\ftwo\fthree")
    doc = {}
    assert list(reader._text_pages(str(p), None, doc)) == [(1, "one"), (2, "two"), (3, "three")]
    assert doc["total"] == 3
    big = tmp_path / "big.txt"
    big.write_text("word " * 400_000)  # 2 MB, one page
    doc = {}
    pages = list(reader._text_pages(str(big), None, doc, limit=1 << 20))
    assert len(pages[0][1]) == 1 << 20 and doc["incomplete"] and doc["total"] is None


def test_streaming_fingerprint_matches_whole_text():
    pages = [" ".join(WORDS[i:] + WORDS[:i]) * 3 for i in range(5)]
    f = similar.Fingerprinter()
    for t in pages:
        f.feed(t)
    assert f.result() == similar.fingerprint("\n".join(pages))
    ws = similar.words("\n".join(pages))  # and the list-based builders
    assert f.result() == {"simhash": f"{similar.simhash(ws):016x}", "minhash": similar.minhash(ws)}
    assert fingerprints.page_text_fp(iter((n + 1, t) for n, t in enumerate(pages))) == similar.fingerprint(
        "\n".join(pages))


# ---------------------------------------------------------------- page-by-page OCR (subprocess mocked)
class FakeTools:
    """Stands in for pdfinfo, pdftotext, pdftoppm, tesseract and magick; records each call."""

    def __init__(self, pages=9, size=(612, 792), image=(1000, 1000)):
        self.pages, self.size, self.image = pages, size, image
        self.calls, self.rendered = [], []

    def __call__(self, cmd, **kw):
        tool = cmd[0]
        self.calls.append(list(cmd))
        out, code = "", 0
        if tool == "pdfinfo":
            first, last = int(cmd[cmd.index("-f") + 1]), int(cmd[cmd.index("-l") + 1])
            out = f"Pages:          {self.pages}\n" + "".join(
                f"Page {n:4d} size: {self.size[0]} x {self.size[1]} pts\n"
                for n in range(first, min(last, self.pages) + 1))
        elif tool == "pdftotext":
            first = int(cmd[cmd.index("-f") + 1]) if "-f" in cmd else 1
            last = int(cmd[cmd.index("-l") + 1]) if "-l" in cmd else self.pages
            out = "\f" * (last - first + 1)  # a scanned PDF: no text layer
        elif tool == "pdftoppm":
            assert cmd[cmd.index("-f") + 1] == cmd[cmd.index("-l") + 1], "one page per render"
            for prev in self.rendered:
                assert not os.path.exists(prev), "the previous page image was freed first"
            img = cmd[-1] + ".png"
            open(img, "wb").close()
            self.rendered.append(img)
        elif tool == "tesseract":
            out = f"page from {os.path.basename(cmd[1])} " + TEXT
        elif tool == "magick" and cmd[1] == "identify":
            out = f"{self.image[0]} {self.image[1]}\n"
        elif tool == "magick":
            open(cmd[-1], "wb").close()

        class R:
            returncode, stdout = code, (out if kw.get("text") else out.encode())
        return R()

    def of(self, tool):
        return [c for c in self.calls if c[0] == tool]


@pytest.fixture
def tools(monkeypatch):
    t = FakeTools()
    monkeypatch.setattr(reader, "_run", t)
    monkeypatch.setattr(reader, "have", lambda name: True)
    return t


def test_ocr_renders_one_page_at_a_time(rec, tools, tmp_path):
    p = put(tmp_path, "scan.pdf", b"%PDF-1.4\n")
    doc = reader.read_document(str(p), "pdf", barcodes=False)
    assert doc["total"] == 9 and [n for n, _ in doc["pages"]] == list(range(1, 10))
    renders = tools.of("pdftoppm")
    assert [c[c.index("-f") + 1] for c in renders] == [str(n) for n in range(1, 10)]
    order = [c[0] for c in tools.calls if c[0] in ("pdftoppm", "tesseract")]
    assert order == ["pdftoppm", "tesseract"] * 9  # render, read, free, then the next page
    assert all(c[c.index("-r") + 1] == str(reader.OCR_DPI) for c in renders)
    assert doc["method"] == "pdftotext+ocr"


def test_fingerprint_reads_at_most_five_pages(rec, tools, tmp_path):
    p = put(tmp_path, "scan.pdf", b"%PDF-1.4\n")
    res = fingerprints.file_fp_info(str(p))
    assert res["fp"] and res["pages"] == 9
    (txt,) = tools.of("pdftotext")
    assert txt[txt.index("-f") + 1] == "1" and txt[txt.index("-l") + 1] == str(fingerprints.FP_PAGES)
    assert len(tools.of("pdftoppm")) == fingerprints.FP_PAGES == len(tools.of("tesseract"))
    assert all(c[c.index("-r") + 1] == str(fingerprints.FP_DPI) for c in tools.of("pdftoppm"))


def test_huge_pages_render_at_a_lower_resolution(rec, tools, tmp_path):
    tools.size = (7200, 7200)  # a 100 x 100 inch page
    p = put(tmp_path, "poster.pdf", b"%PDF-1.4\n")
    reader.read_document(str(p), "pdf", pages=[1], barcodes=False)
    (c,) = tools.of("pdftoppm")
    dpi = int(c[c.index("-r") + 1])
    assert dpi < reader.OCR_DPI and (100 * dpi) ** 2 <= reader.MAX_OCR_PIXELS


def test_large_image_is_downscaled_before_ocr(rec, tools, tmp_path):
    tools.image = (10000, 8000)  # 80 MP
    p = put(tmp_path, "photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 50)
    reader.read_document(str(p), "jpeg", barcodes=False)
    (conv,) = [c for c in tools.of("magick") if c[1] != "identify"]
    assert conv[conv.index("-resize") + 1] == f"{reader.MAX_OCR_PIXELS}@>" and "-limit" in conv
    (t,) = tools.of("tesseract")
    assert t[1] == conv[-1]


def test_small_image_is_not_converted(rec, tools, tmp_path):
    p = put(tmp_path, "photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 50)
    reader.read_document(str(p), "jpeg", barcodes=False)
    assert [c[1] for c in tools.of("magick")] == ["identify"] and tools.of("tesseract")[0][1] == str(p)


def test_failed_ocr_leaves_the_file_unscreened(rec, monkeypatch, capsys):
    def failing(cmd, **kw):
        class R:
            returncode, stdout = (1 if cmd[0] == "tesseract" else 0), ""
        return R()
    monkeypatch.setattr(reader, "_run", failing)
    monkeypatch.setattr(reader, "have", lambda name: True)
    put(rec, "inbox/photo.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 50)
    host.main(["scan"])
    out = capsys.readouterr().out
    assert "UNSCREENED" in out and reader.OCR_FAILED in out


# ---------------------------------------------------------------- --profile-mem
def test_profile_mem_logs_names_and_numbers_never_content(rec, capsys):
    put(rec, "Archive/notes.txt", TEXT + " " + SENTINEL)
    put(rec, "Media/clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    put(rec, "inbox/new notes.txt", TEXT + " other " + SENTINEL)
    assert host.main(["scan", "--profile-mem"]) == 0
    cap = capsys.readouterr()
    lines = [x for x in cap.err.splitlines() if x.startswith("mem: ")]
    assert any("Archive/notes.txt" in x and "type=text" in x and "result=fingerprinted" in x
               and "peak_rss_mb=" in x for x in lines)
    assert any("Media/clip.mp4" in x and "sha256 only" in x for x in lines)
    assert any("inbox/new notes.txt" in x and "method=text" in x for x in lines)
    assert any(x.startswith("mem: total") for x in lines)
    names = cap.err.replace("new notes.txt", "").replace("notes.txt", "")
    for w in SENTINEL.split() + WORDS:
        assert w not in names
    assert "zebrafinch" not in cap.out
