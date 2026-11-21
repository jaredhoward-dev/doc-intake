"""`doctor` (doc_intake/cli.py): reports the setup without reading a document, creating a folder or writing a log.
Tools, the memory-cap probe and the model server are faked both ways; fake data only."""
import os
import socket
import sys

import pytest

from doc_intake import __version__, cli, llm, reader

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "fixtures", "profiles")
FIXTURE_PROFILES = sorted(n[:-5] for n in os.listdir(PROFILES) if n.endswith(".json") and n != "senders.json")


def config(tmp_path, extra=""):
    root = tmp_path / "Records"
    root.mkdir(mode=0o700, exist_ok=True)
    p = tmp_path / "doc-intake.toml"
    p.write_text(f'root = "{root}"\nprofiles_dir = "{PROFILES}"\nsenders_file = "{PROFILES}/senders.json"\n' + extra)
    return p, root


def tree(folder):
    return sorted(os.path.relpath(os.path.join(d, n), folder) for d, ds, fs in os.walk(folder) for n in ds + fs)


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to reach a model server, or open a socket at all, fails the test."""
    def boom(*a, **k):
        raise AssertionError("doctor contacted the network")
    monkeypatch.setattr(llm, "reachable", boom)
    monkeypatch.setattr(llm._OPENER, "open", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.fixture
def tools(monkeypatch):
    """Fake every optional tool present (ImageMagick 7), the cap applying, the model server up; tests flip one."""
    state = {"have": {"pdftotext", "pdftoppm", "tesseract", "magick", "convert", "identify"},
             "modules": {"zxingcpp", "PIL"}, "capped": True}
    monkeypatch.setattr(reader, "have", lambda t: t in state["have"])
    monkeypatch.setattr(cli, "_importable", lambda m: m in state["modules"])
    monkeypatch.setattr(reader, "run_bounded", lambda fn, *a, **k: (None, {"ok": True, "capped": state["capped"]}))
    monkeypatch.delenv(cli.ENV, raising=False)
    return state


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def test_good_config(tmp_path, tools, monkeypatch, capsys):
    p, _ = config(tmp_path, 'model = "example-model"\n[[route]]\ncategory = "receipt"\npatterns = ["receipt"]\n')
    monkeypatch.setattr(llm, "reachable", lambda: True)
    code, lines, err = run(["--config", str(p), "doctor"], capsys)
    assert code == 0 and err == ""
    v = sys.version_info
    assert lines[:4] == [f"python: {v.major}.{v.minor}.{v.micro} ok", f"doc-intake: {__version__}",
                         f"config: {p} (found by --config)", "config loads: ok"]
    for want in ("root: ok", "inbox: missing", "state folder: missing", "senders_file: ok",
                 "privacy_terms_file: not configured", "routing rules: 1", "pdftotext: ok", "pdftoppm: ok",
                 "tesseract: ok", "imagemagick: ok (7)", "zxing: ok", "pillow: ok", "memory cap: ok",
                 "model: example-model", "model server: ok",
                 f"profiles_dir: ok ({len(FIXTURE_PROFILES)} profile(s): {', '.join(FIXTURE_PROFILES)})"):
        assert want in lines
    assert not any(ln.startswith("profiles_dir") and "senders" in ln for ln in lines)   # never a profile


def test_existing_folders_and_how_the_config_was_found(tmp_path, tools, no_network, monkeypatch, capsys):
    p, root = config(tmp_path)
    (root / "inbox").mkdir()
    (root / ".doc-intake").mkdir()
    monkeypatch.setenv(cli.ENV, str(p))
    code, lines, _ = run(["doctor"], capsys)
    assert code == 0 and f"config: {p} (found by ${cli.ENV})" in lines
    assert "inbox: ok" in lines and "state folder: ok" in lines
    monkeypatch.delenv(cli.ENV)
    monkeypatch.chdir(tmp_path)
    code, lines, _ = run(["doctor"], capsys)
    assert code == 0 and f"config: {p} (found by ./doc-intake.toml)" in lines


def test_profiles_and_senders_not_configured(tmp_path, tools, no_network, capsys):
    root = tmp_path / "Records"
    root.mkdir()
    p = tmp_path / "doc-intake.toml"
    p.write_text(f'root = "{root}"\n')
    code, lines, _ = run(["--config", str(p), "doctor"], capsys)
    assert code == 0
    assert "profiles_dir: not configured" in lines and "senders_file: not configured" in lines


def test_missing_root_is_exit_2_with_the_clean_message(tmp_path, tools, no_network, capsys):
    p, root = config(tmp_path)
    root.rmdir()
    code, lines, err = run(["--config", str(p), "doctor"], capsys)
    assert code == 2
    assert err.strip() == f"doc-intake: root does not exist: {root} (set in doc-intake.toml)"
    assert "Traceback" not in err and "root: missing" in lines
    assert "pdftotext: ok" in lines and "model: not configured" in lines   # the rest is still reported
    assert not root.exists()


def test_config_error_is_exit_2_with_the_clean_message(tmp_path, tools, no_network, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, lines, err = run(["doctor"], capsys)
    assert code == 2 and err.startswith("doc-intake: config: no config file") and "Traceback" not in err
    assert "model: unknown (config did not load)" in lines and "config loads: ok" not in lines
    p, _ = config(tmp_path, "profile_dir = 'x'\n")
    code, lines, err = run(["--config", str(p), "doctor"], capsys)
    assert code == 2 and err.startswith("doc-intake: config: unknown key") and "profile_dir" in err


def test_tools_absent(tmp_path, tools, no_network, capsys):
    tools["have"], tools["modules"] = set(), set()
    p, _ = config(tmp_path)
    code, lines, _ = run(["--config", str(p), "doctor"], capsys)
    assert code == 0   # missing optional tools are informational
    for t in ("pdftotext", "pdftoppm", "tesseract", "imagemagick", "zxing", "pillow"):
        assert f"{t}: missing" in lines


def test_imagemagick_6_and_7(tmp_path, tools, no_network, capsys):
    p, _ = config(tmp_path)
    tools["have"] = {"convert", "identify"}
    assert "imagemagick: ok (6)" in run(["--config", str(p), "doctor"], capsys)[1]
    tools["have"] = {"convert"}   # a lone convert (Windows' disk converter) is not ImageMagick
    assert "imagemagick: missing" in run(["--config", str(p), "doctor"], capsys)[1]


@pytest.mark.parametrize("capped, want", [(True, "memory cap: ok"),
                                          (False, "memory cap: not available on this platform"),
                                          (None, "memory cap: unknown (probe failed)")])
def test_memory_cap_probe(capped, want, tmp_path, tools, no_network, capsys):
    tools["capped"] = capped
    p, _ = config(tmp_path)
    code, lines, _ = run(["--config", str(p), "doctor"], capsys)
    assert code == 0 and any(ln.startswith(want) for ln in lines)


def test_memory_cap_probe_reads_nothing(monkeypatch):
    """The real probe: one bounded run of a function that takes no argument, so no path can reach the child; its
    capped info is what the platform does, and the probe is not counted as an uncapped read."""
    seen = []
    real = reader.run_bounded

    def spy(fn, *args, **kw):
        seen.append(args)
        return real(fn, *args, **kw)
    monkeypatch.setattr(reader, "run_bounded", spy)
    reader.reset_uncapped()
    got = cli.memory_cap()
    assert seen == [()] and got in (True, False)
    if sys.platform == "darwin":
        assert got is False   # macOS refuses RLIMIT_AS
    elif sys.platform.startswith("linux"):
        assert got is True
    assert reader.uncapped_reads() == 0


def test_model_not_configured_contacts_nothing(tmp_path, tools, no_network, capsys):
    p, _ = config(tmp_path)
    code, lines, _ = run(["--config", str(p), "doctor"], capsys)
    assert code == 0 and "model: not configured" in lines
    assert not any(ln.startswith("model server") for ln in lines)


def test_no_model_flag_skips_the_server(tmp_path, tools, no_network, capsys):
    p, _ = config(tmp_path, 'model = "example-model"\n')
    code, lines, _ = run(["--config", str(p), "doctor", "--no-model"], capsys)
    assert code == 0 and "model server: skipped (--no-model)" in lines


def test_model_server_down(tmp_path, tools, monkeypatch, capsys):
    """A real request to 127.0.0.1 on a port nothing listens on: not reachable, and still exit 0."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(llm, "OLLAMA_PORT", port)
    p, _ = config(tmp_path, 'model = "example-model"\n')
    code, lines, _ = run(["--config", str(p), "doctor"], capsys)
    assert code == 0 and f"model server: not reachable on 127.0.0.1:{port}" in lines


def test_doctor_creates_nothing(tmp_path, tools, no_network, capsys):
    p, root = config(tmp_path, 'inbox = "drop"\nstate = ".state"\n')
    before = tree(tmp_path)
    code, lines, _ = run(["--config", str(p), "doctor"], capsys)
    assert code == 0 and "inbox: missing" in lines and "state folder: missing" in lines
    assert tree(tmp_path) == before and list(root.iterdir()) == []


def test_module_form_runs_doctor(tmp_path, tools, no_network, monkeypatch, capsys):
    import runpy
    p, _ = config(tmp_path)
    monkeypatch.setattr("sys.argv", ["doc_intake", "--config", str(p), "doctor"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("doc_intake", run_name="__main__")
    assert e.value.code == 0 and "config loads: ok" in capsys.readouterr().out
