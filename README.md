# doc-intake

Local-only document intake for working with an AI assistant without it seeing your documents.

Documents land in an inbox folder. doc-intake reads each one on your machine, counts what its
detectors find (card, account and routing numbers, SSNs, keys, passwords, 2FA codes, ...) without
printing a value, holds files that carry secret material, spots exact and near duplicates, and
pulls out only the fields a profile allows. You preview those fields and approve them with a click
on your own desktop; only then can the assistant read them, and every read is logged.

- **Everything runs locally.** Standard-library Python plus optional system tools (`pdftotext`,
  `pdftoppm`, `tesseract`, ImageMagick 7, zxing-cpp). An optional local model through your own
  Ollama on `127.0.0.1` fills fields the patterns missed; nothing is sent anywhere.
- **Allowlisted fields only.** A profile names the fields to take; each value is checked against the
  detectors and dropped (and counted) if it overlaps one.
- **Approval is yours.** Approving shows a desktop dialog; the approval seals the values with a
  sha256, and a changed task file or source document is refused.

## Status

Stage 2: the engine has moved here from its first host, a private records tool, and now runs on
its own through a minimal CLI over a config file. There is no packaged release yet; see
[ROADMAP.md](ROADMAP.md) and [docs/HANDOFF.md](docs/HANDOFF.md). The module guide is
[docs/engine.md](docs/engine.md).

## Running it

Python 3.11 or newer. Copy [examples/doc-intake.toml](examples/doc-intake.toml), set `root` to your
records folder (and `profiles_dir` / `senders_file`, which are relative to the config file), then from
a checkout:

    python3 -m doc_intake --config path/to/doc-intake.toml scan
    python3 -m doc_intake fields --task receipt inbox/some-receipt.pdf
    python3 -m doc_intake status

The `receipt` task is one of the example profiles in [profiles/](profiles/); [docs/profiles.md](docs/profiles.md)
shows how to write your own.

The config is found by `--config`, else `$DOC_INTAKE_CONFIG`, else `./doc-intake.toml`. You preview
and approve in your own terminal (`preview`, then `approve`); the assistant only ever runs `read`
with a reason. Commands, config keys and routing rules: [docs/cli.md](docs/cli.md).

## Tests

    python3 -I -m pytest -q tests

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
