# Roadmap

## Stage 1: the engine moves in (done)

- The engine modules as the `doc_intake` package, unchanged apart from the package name.
- CI on synthetic data, the import allowlist, and a forbidden-terms check a host feeds its own list.

## Stage 2: usable on its own

- Done: port the engine's tests from the first host, against a small fake host (`tests/host.py`: a
  temporary records root, inbox and state folder, and example profiles under `tests/fixtures/`).
- Done: example profiles (`profiles/receipt.json`, `profiles/invoice.json`) and a known-senders example
  with fake vendors. See [docs/profiles.md](docs/profiles.md).
- Done: a minimal CLI (`python3 -m doc_intake scan | fields | preview | approve | status | read`, plus
  `keep`, `extract`, `cleanup` and `pairs`) over a TOML config file, so it runs without writing a host.
  See [docs/cli.md](docs/cli.md).
- Done: `pyproject.toml` (stdlib only, a `doc-intake` console script, a `barcodes` extra for zxing-cpp
  and Pillow). Hosts install from a checkout or a git tag.
- A tagged release (`v0.1.0`) that hosts pin.

## Stage 3: better extraction

- Side-by-side boxes on scanned forms (a Truth in Lending box: five figures in a row under their
  headers) where OCR columns don't line up with the labels; today only the local model fills them.
- More detectors and profiles, each with fake fixtures.
- A review-page option to mark a value wrong, which re-runs fields for that document.

## Not planned

- Any cloud OCR or hosted model.
- Storing document text: the state folder holds fingerprints, counts and approved fields only.
