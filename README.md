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

Stage 1: the engine has moved here from its first host, a private records tool, and is not yet
usable on its own. There is no CLI, no example host and no packaged release yet; see
[ROADMAP.md](ROADMAP.md) and [docs/HANDOFF.md](docs/HANDOFF.md). The module guide is
[docs/engine.md](docs/engine.md).

## Tests

    python3 -I -m unittest discover -s tests -t . -v

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
