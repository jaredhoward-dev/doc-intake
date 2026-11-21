# Roadmap

## Stage 1: the engine moves in (this pull request)

- The engine modules as the `doc_intake` package, unchanged apart from the package name.
- CI on synthetic data, the import allowlist, and a forbidden-terms check a host feeds its own list.

## Stage 2: usable on its own

- Port the engine's tests from the first host, against a small fake host (`tests/host.py`: a
  temporary records root, inbox and state folder, and example profiles under `tests/fixtures/`).
- Example profiles (`profiles/receipt.json`, `profiles/invoice.json`) and a known-senders example
  with fake vendors.
- A minimal CLI (`python3 -m doc_intake scan | fields | preview | approve | status | read`) over a
  config file, so it runs without writing a host.
- `pyproject.toml` and a tagged release (`v0.1.0`) that hosts pin.

## Stage 3: better extraction

- Side-by-side boxes on scanned forms (a Truth in Lending box: five figures in a row under their
  headers) where OCR columns don't line up with the labels; today only the local model fills them.
- More detectors and profiles, each with fake fixtures.
- A review-page option to mark a value wrong, which re-runs fields for that document.

## Not planned

- Any cloud OCR or hosted model.
- Storing document text: the state folder holds fingerprints, counts and approved fields only.
