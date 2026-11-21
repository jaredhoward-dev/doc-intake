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

- Done: side-by-side boxes on scanned forms (a Truth in Lending box: five figures in a row under
  their headers) with the opt-in `box_row` key ([docs/profiles.md](docs/profiles.md)); still to be
  confirmed on real scans.
- More detectors and profiles, each with fake fixtures. Done so far: card numbers in dotted groups;
  a `utility_bill` example profile and route.
- Done: a `doctor` command that checks the config, tools, memory cap and model without reading a
  document ([docs/cli.md](docs/cli.md)).
- Done: a review-page option to mark a value wrong, which re-runs fields for that document: the page shows a
  `wrong` command beside each value, for the user's own terminal (see the flow in
  [docs/engine.md](docs/engine.md)).

## Not planned

- Any cloud OCR or hosted model.
- Storing document text: the state folder holds fingerprints, counts and approved fields only.
