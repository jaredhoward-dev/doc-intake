"""The minimal CLI (doc_intake/cli.py): finding and checking the config file, the simple routing, and an end-to-end
run through cli.main on a throwaway root. Fake data only; conftest's fresh_config puts the fake host back after
each test, and its no_real_dialog stands in for the desktop click."""
import json
import os

import pytest

from doc_intake import CFG, cli, places, reader
from doc_intake.cli import ConfigError
from doc_intake.scan import NO_PROFILE

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "fixtures", "profiles")
REPO = os.path.dirname(HERE)

RECEIPT = """Example Corner Shop
Receipt
Transaction date: March 3, 2030
Subtotal   $11.00
Total      $12.34
Amount paid $12.34
"""


def write_config(folder, body):
    p = folder / "doc-intake.toml"
    p.write_text(body)
    return p


def base_config(tmp_path, extra=""):
    root = tmp_path / "Records"
    root.mkdir(mode=0o700, exist_ok=True)
    return write_config(tmp_path, f'root = "{root}"\nprofiles_dir = "{PROFILES}"\n'
                                  f'senders_file = "{PROFILES}/senders.json"\n' + extra)


# ---------------------------------------------------------------- finding the config
def test_found_by_flag_then_env_then_cwd(tmp_path, monkeypatch):
    a, b, c = (tmp_path / n for n in "abc")
    for d in (a, b, c):
        d.mkdir()
        write_config(d, 'root = "."\n')
    env = {cli.ENV: str(b / "doc-intake.toml")}
    assert cli.find_config(str(a / "doc-intake.toml"), env, str(c)) == str(a / "doc-intake.toml")
    assert cli.find_config(None, env, str(c)) == str(b / "doc-intake.toml")
    assert cli.find_config(None, {}, str(c)) == str(c / "doc-intake.toml")
    monkeypatch.chdir(c)
    monkeypatch.delenv(cli.ENV, raising=False)
    assert cli.find_config() == str(c / "doc-intake.toml")


def test_missing_config_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="no config file"):
        cli.find_config(None, {}, str(tmp_path))
    with pytest.raises(ConfigError, match="--config"):
        cli.find_config(str(tmp_path / "nope.toml"), {}, str(tmp_path))
    with pytest.raises(ConfigError, match=cli.ENV):
        cli.find_config(None, {cli.ENV: str(tmp_path / "nope.toml")}, str(tmp_path))


def test_main_reports_config_errors_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(cli.ENV, raising=False)
    assert cli.main(["status"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("doc-intake: config: no config file") and "Traceback" not in err


# ---------------------------------------------------------------- checking the config
def test_relative_paths_resolve_against_the_config_folder(tmp_path, monkeypatch):
    conf = tmp_path / "conf"
    (conf / "profiles").mkdir(parents=True)
    (conf / "profiles" / "senders.json").write_text("{}")
    (conf / "terms.txt").write_text("# a comment\nExample Family\n\n")
    p = write_config(conf, 'root = "../Records"\nprofiles_dir = "profiles"\nsenders_file = "profiles/senders.json"\n'
                           'privacy_terms_file = "terms.txt"\ninbox = "drop"\nstate = ".state"\nmodel = "m1"\n')
    monkeypatch.chdir(tmp_path / "conf" / "profiles")  # the working folder plays no part
    s = cli.load_config(str(p))
    assert s["root"] == str(tmp_path / "Records")
    assert s["profiles_dir"] == str(conf / "profiles")
    assert s["senders_file"] == str(conf / "profiles" / "senders.json")
    assert (s["inbox"], s["state"], s["model"]) == ("drop", ".state", "m1")
    assert cli.make_privacy_terms(s["privacy_terms_file"])() == ["example family"]


def test_root_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = write_config(tmp_path, 'root = "~/Records"\n')
    assert cli.load_config(str(p))["root"] == str(tmp_path / "Records")


@pytest.mark.parametrize("body, msg", [
    ('root = "."\nprofile_dir = "x"\n', "unknown key.*profile_dir"),
    ('inbox = "in"\n', "root is required"),
    ('root = "."\nroot2 = 1\n', "unknown key.*root2"),
    ('root = 5\n', "root must be"),
    ('root = "."\ninbox = "../out"\n', "inbox must be a folder name under root"),
    ('root = "."\nprofiles_dir = "missing"\n', "profiles_dir does not exist"),
    ('root = "."\nprivacy_terms_file = "missing.txt"\n', "privacy_terms_file does not exist"),
    ('root = "."\n[[route]]\ncategory = "receipt"\n', r"route\[0\]\.patterns is required"),
    ('root = "."\n[[route]]\ncategory = "receipt"\npatterns = ["("]\n', r"route\[0\]\.patterns\[0\] is not a valid"),
    ('root = "."\n[[route]]\ncategory = "receipt"\npatterns = ["x"]\ndest = "R/{month}"\n', "only {year}"),
    ('root = "."\n[[route]]\ncategory = "receipt"\npatterns = ["x"]\ndest = "/abs/{year}"\n', "folder under root"),
    ('root = "."\n[[route]]\ncategory = "receipt"\npatterns = ["x"]\nprofle = "receipt"\n',
     r"unknown key in route\[0\]: profle"),
    ('root = "."\n[[route]]\ncategory = "receipt"\npatterns = ["x"]\nmin_matches = 2\n', "min_matches"),
    ('root = "."\nroute = "receipt"\n', "route must be a list"),
])
def test_bad_config_names_the_key(tmp_path, body, msg):
    with pytest.raises(ConfigError, match=msg):
        cli.load_config(str(write_config(tmp_path, body)))


def test_unknown_profile_in_a_route_is_an_error(tmp_path):
    p = base_config(tmp_path, '[[route]]\ncategory = "receipt"\npatterns = ["x"]\nprofile = "no_such"\n')
    with pytest.raises(ConfigError, match=r"route\[0\]\.profile.*no_such"):
        cli.load_config(str(p))


def test_bad_toml_is_a_clear_error(tmp_path, capsys):
    p = write_config(tmp_path, 'root = "unclosed\n')
    with pytest.raises(ConfigError, match="not valid TOML"):
        cli.load_config(str(p))
    assert cli.main(["--config", str(p), "status"]) == 2
    err = capsys.readouterr().err
    assert "not valid TOML" in err and "Traceback" not in err


def test_privacy_terms_are_never_printed(tmp_path, capsys):
    (tmp_path / "terms.txt").write_text("Example Secret Word\n")
    p = base_config(tmp_path, 'privacy_terms_file = "terms.txt"\n')
    assert cli.main(["--config", str(p), "status"]) == 0
    assert CFG.privacy_terms() == ["example secret word"]
    out = capsys.readouterr()
    assert "secret word" not in (out.out + out.err).lower()


# ---------------------------------------------------------------- routing
RULES = """
[[route]]
category = "invoice"
patterns = ['\\binvoice\\b', '\\bamount\\s+due\\b']
min_matches = 2
profile = "invoice"
dest = "Bills/{year}"

[[route]]
category = "receipt"
patterns = ['\\breceipt\\b', '\\btotal\\b']
profile = "receipt"
dest = "Spending/{year}"
name = "Shop receipt"

[[route]]
category = "letter"
patterns = ['\\bdear\\b']
"""


def test_routing_rules_first_match_wins(tmp_path):
    s = cli.load_config(str(base_config(tmp_path, RULES)))
    f = tmp_path / "a.txt"
    f.write_text("x")
    route = cli.make_route(s["route"])
    r = route(str(f), "text", RECEIPT)
    assert (r["category"], r["profile"], r["dest"]) == ("receipt", "receipt", "Spending/2030")
    assert r["name"] == "2030-03-03 - Shop receipt.txt" and not r["date_from_file"]
    assert r["note"] == "next: fields --task receipt"
    assert route(str(f), "text", RECEIPT, year="2031")["dest"] == "Spending/2031"
    # one invoice word is below min_matches, so the receipt rule (total) takes it
    assert route(str(f), "text", "Invoice\nTotal $5.00\n")["category"] == "receipt"
    r = route(str(f), "text", "Invoice\nAmount due $5.00\nTotal $5.00\n")
    assert (r["category"], r["dest"]) == ("invoice", "Bills/" + r["name"][:4])
    r = route(str(f), "text", "Dear someone, hello")
    assert (r["category"], r["profile"], r["dest"], r["note"]) == ("letter", None, None, NO_PROFILE)
    assert r["name"].endswith(" - Letter.txt")


def test_no_rule_matching_is_the_engines_default(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    for rules in ([], cli.load_config(str(base_config(tmp_path, RULES)))["route"]):
        r = cli.make_route(rules)(str(f), "text", "nothing to see")
        assert (r["category"], r["profile"], r["dest"], r["name"]) == ("unknown", None, None, "notes.txt")
        assert r["note"] == NO_PROFILE


# ---------------------------------------------------------------- end to end
@pytest.fixture
def e2e(tmp_path, monkeypatch):
    monkeypatch.setattr(places, "is_tty", lambda: False)
    monkeypatch.setattr(reader, "_zxing", lambda: None)
    monkeypatch.delenv(cli.ENV, raising=False)
    p = base_config(tmp_path, RULES)
    root = tmp_path / "Records"
    (root / "inbox").mkdir(mode=0o700)
    (root / "inbox" / "shop.txt").write_text(RECEIPT)
    return p, root


def approve(argv, monkeypatch):
    """approve as if at the user's own terminal (it refuses anywhere else); conftest fakes the desktop click."""
    with monkeypatch.context() as m:
        m.setattr(places, "is_tty", lambda: True)
        return cli.main(argv)


def test_end_to_end(e2e, monkeypatch, capsys):
    p, root = e2e
    c = ["--config", str(p)]
    assert cli.main(c + ["scan"]) == 0
    out = capsys.readouterr().out
    assert "shop.txt" in out and "Spending/2030" in out and "$12.34" not in out
    assert CFG.cli == "python3 -m doc_intake"

    assert cli.main(c + ["fields", "--task", "receipt", "inbox/shop.txt"]) == 0
    out = capsys.readouterr().out
    assert "python3 -m doc_intake approve" in out and "12.34" not in out
    task = sorted((root / ".doc-intake" / "tasks").iterdir())[-1]

    assert cli.main(c + ["status"]) == 0
    out = capsys.readouterr().out
    assert task.name in out and "12.34" not in out

    assert cli.main(c + ["read", task.name, "--reason", "test"]) == 2   # not approved yet
    assert "is not approved" in capsys.readouterr().err

    assert cli.main(c + ["approve", task.name]) == 2   # not from the user's terminal
    assert "own terminal" in capsys.readouterr().err

    assert approve(c + ["approve", task.name], monkeypatch) == 0
    assert capsys.readouterr().out.startswith(f"approved: {task.name}")

    assert cli.main(c + ["read", task.name, "--reason", "test"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["task"] == "receipt" and any(v["value"] == "$12.34" or "12.34" in v["value"] for v in got["values"])
    log = (root / ".doc-intake" / "access.log").read_text()
    assert "\tread\t" in log and "reason=test" in log


def test_read_still_needs_a_reason(e2e, capsys):
    p, _ = e2e
    with pytest.raises(SystemExit):
        cli.main(["--config", str(p), "read", "x.json"])
    assert cli.main(["--config", str(p), "read", "x.json", "--reason", " "]) == 2


def test_env_config_and_missing_root(e2e, tmp_path, monkeypatch, capsys):
    p, root = e2e
    monkeypatch.setenv(cli.ENV, str(p))
    assert cli.main(["status"]) == 0
    assert "no task files" in capsys.readouterr().out
    os.rename(root, tmp_path / "gone")
    assert cli.main(["status"]) == 2
    assert "root does not exist" in capsys.readouterr().err


def test_main_module_runs_help():
    import subprocess
    import sys
    code = (f"import sys, runpy; sys.path.insert(0, {REPO!r}); sys.argv = ['doc_intake', '--help'];"
            "runpy.run_module('doc_intake', run_name='__main__')")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--config" in r.stdout and "approve" in r.stdout


def test_example_config_is_valid():
    """examples/doc-intake.toml parses, uses only known keys, and its rules compile; with the example profiles
    present it loads in full."""
    import tomllib
    path = os.path.join(REPO, "examples", "doc-intake.toml")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    assert set(data) <= cli.KEYS and "root" in data
    rules = cli._rules(data["route"])
    assert [r["category"] for r in rules] == ["invoice", "receipt"]
    if os.path.isfile(os.path.join(REPO, "profiles", "receipt.json")):
        assert cli.load_config(path)["profiles_dir"] == os.path.join(REPO, "profiles")
