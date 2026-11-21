"""The engine (doc_intake/) stands alone: it imports with nothing of a host on the path, and holds none of the
test host's own layout or settings (a host passes those in through configure()).

tests/test_generic.py covers the import allowlist and a host's private terms; this adds the checks it lacks.
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "doc_intake")


def modules():
    return sorted("doc_intake." + n[:-3] for n in os.listdir(PKG) if n.endswith(".py") and n != "__init__.py")


def test_engine_has_its_core_modules():
    names = set(os.listdir(PKG))
    assert {"__init__.py", "config.py", "detect.py", "reader.py", "scan.py", "approval.py", "tasks.py"} <= names


def test_engine_imports_alone():
    """Every engine module imports in a fresh interpreter with only the repo root on the path and no configure():
    nothing of the test host (host, helpers, conftest) or of pytest is needed."""
    code = (f"import sys; sys.path.insert(0, {REPO!r}); import doc_intake, {', '.join(modules())};"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('host', 'helpers', 'conftest', 'tests',"
            " 'pytest', '_pytest'));"
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=PKG, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_test_host_layout_or_settings_in_the_engine():
    """The fake host's folders, env var and file names live in tests/host.py only, never in the engine."""
    patterns = [r"DOC_INTAKE_TEST_ROOT", r"tests/host", r"\bhost\.py\b", r"\bTax/", r"\bPay/", r"\bExpenses/"]
    # (".doc-intake" is the engine's own default state folder name, so it is not on this list)
    hits = {}
    for n in sorted(os.listdir(PKG)):
        if not n.endswith(".py"):
            continue
        text = open(os.path.join(PKG, n), encoding="utf-8").read()
        k = sum(bool(re.search(p, text)) for p in patterns)
        if k:
            hits[n] = k
    assert hits == {}
