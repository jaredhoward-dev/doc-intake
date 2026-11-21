"""pyproject.toml: it parses, its version is doc_intake.__version__, the console script points at a callable that
returns the exit status (setuptools' generated script calls sys.exit on it), only the engine package ships, and
requires-python matches what the code needs (tomllib, so 3.11). No install here: CI has no pip."""
import importlib
import inspect
import os
import tomllib

import pytest

import doc_intake

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "doc_intake")


@pytest.fixture(scope="module")
def project():
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def test_version_matches_package(project):
    assert "version" not in project["project"].get("dynamic", [])
    assert project["project"]["version"] == doc_intake.__version__


def test_no_runtime_dependencies(project):
    assert project["project"].get("dependencies", []) == []
    assert {d.lower() for d in project["project"]["optional-dependencies"]["barcodes"]} == {"zxing-cpp", "pillow"}


def test_console_script_target_is_callable(project, tmp_path, monkeypatch, capsys):
    target = project["project"]["scripts"]["doc-intake"]
    module, _, attr = target.partition(":")
    main = getattr(importlib.import_module(module), attr)
    assert callable(main)
    assert all(p.default is not inspect.Parameter.empty for p in inspect.signature(main).parameters.values())
    # It returns the status rather than exiting, so the generated script's sys.exit(main()) passes it on.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOC_INTAKE_CONFIG", raising=False)
    assert main(["status"]) == 2
    assert "no config file" in capsys.readouterr().err


def test_only_the_engine_package_ships(project):
    assert project["tool"]["setuptools"]["packages"] == ["doc_intake"]
    assert os.path.isfile(os.path.join(PKG, "__init__.py"))
    # No subpackages left out by the explicit list.
    assert not [d for d in os.listdir(PKG) if os.path.isfile(os.path.join(PKG, d, "__init__.py"))]


def test_license_matches_the_license_file(project):
    assert project["project"]["license"] == "AGPL-3.0-or-later"
    with open(os.path.join(REPO, project["project"]["license-files"][0]), encoding="utf-8") as f:
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in f.readline()


def test_requires_python_matches_the_code(project):
    assert project["project"]["requires-python"] == ">=3.11"
    uses_tomllib = any("import tomllib" in open(os.path.join(PKG, n), encoding="utf-8").read()
                       for n in os.listdir(PKG) if n.endswith(".py"))
    assert uses_tomllib  # tomllib is new in 3.11; drop the floor only with it
