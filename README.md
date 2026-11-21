# doc-intake

Local-only document intake for working with an AI assistant without it seeing your documents.

Documents land in an inbox folder. doc-intake reads each one on your machine, counts what its
detectors find (card, account and routing numbers, SSNs, keys, passwords, 2FA codes, ...) without
printing a value, holds files that carry secret material, spots exact and near duplicates, and
pulls out only the fields a profile allows. You preview those fields and approve them with a click
on your own desktop; only then can the assistant read them, and every read is logged.

- **Everything runs locally.** Standard-library Python plus optional system tools (`pdftotext`,
  `pdftoppm`, `tesseract`, ImageMagick 7 or 6, zxing-cpp). An optional local model through your own
  Ollama on `127.0.0.1` fills fields the patterns missed; nothing is sent anywhere.
- **Allowlisted fields only.** A profile names the fields to take; each value is checked against the
  detectors and dropped (and counted) if it overlaps one.
- **Approval is yours.** Approving shows a desktop dialog; the approval seals the values with a
  sha256, and a changed task file or source document is refused. Marking a value wrong (`wrong`) is
  yours too: only from your own terminal, and the result is approved again.

## Status

Stage 2: the engine has moved here from its first host, a private records tool, and now runs on
its own through a minimal CLI over a config file. It installs with pip (`pyproject.toml`); the
first tagged release, `v0.1.0`, is still to come. See
[ROADMAP.md](ROADMAP.md) and [docs/HANDOFF.md](docs/HANDOFF.md). The module guide is
[docs/engine.md](docs/engine.md).

## Running it

Python 3.11 or newer, no dependencies. Run it from a checkout as `python3 -m doc_intake`, or install
it, which adds a `doc-intake` command:

    pip install .                      # from a checkout
    pip install '.[barcodes]'          # plus zxing-cpp and Pillow, for QR and barcode checks
    pip install git+https://github.com/jaredhoward-dev/doc-intake@v0.1.0   # a pinned tag, once tagged

The example config and profiles are not part of the installed package: copy
[examples/doc-intake.toml](examples/doc-intake.toml) and [profiles/](profiles/) from the repo. Set
`root` to your records folder (and `profiles_dir` / `senders_file`, which are relative to the config
file), then:

    python3 -m doc_intake --config path/to/doc-intake.toml scan
    python3 -m doc_intake fields --task receipt inbox/some-receipt.pdf
    python3 -m doc_intake status

`doc-intake scan` and so on are the same commands once installed.

The `receipt` task is one of the example profiles in [profiles/](profiles/) (with `invoice` and
`utility_bill`); [docs/profiles.md](docs/profiles.md)
shows how to write your own.

The config is found by `--config`, else `$DOC_INTAKE_CONFIG`, else `./doc-intake.toml`. You preview
and approve in your own terminal (`preview`, then `approve`, or `wrong` to mark a value wrong and
re-run that document); the assistant only ever runs `read` with a reason. `doctor` checks the setup
without reading a document. Commands, config keys and routing rules: [docs/cli.md](docs/cli.md).

## Tests

    python3 -I -m pytest -q tests

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
