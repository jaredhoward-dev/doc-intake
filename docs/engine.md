# doc-intake engine

Screen, fingerprint and read personal documents so an AI assistant can work with them without seeing them.

Documents land in an inbox folder. The engine reads each one locally, counts what its detectors find (card,
account and routing numbers, SSNs, keys, passwords, 2FA codes, ...) without ever printing a value, holds files
that carry secret material, spots exact and near duplicates of what the records folder already holds, and asks the
host program where each document should go. Values reach the assistant only as an allowlisted set of fields that
the user has previewed and approved, and every read is logged.

The engine holds no folder layout, no vendor list and no bookkeeping. A host program (a small CLI of its own)
configures it with a records root, an inbox, its field profiles and a routing hook, and keeps any filing or
accounting logic to itself.

## Status

Stage 2: the engine runs on its own through `python3 -m doc_intake` over a TOML config ([cli.md](cli.md)), or
inside a host that calls `configure()`. The interfaces below may still change before a packaged release.

## What it does

| Module | What |
| --- | --- |
| `cli.py`, `__main__.py` | The standalone CLI: reads a TOML config (`--config`, `$DOC_INTAKE_CONFIG`, `./doc-intake.toml`), calls `configure()` and runs the commands |
| `config.py` | `configure()`: the records root, inbox, state folder, profiles, known-senders map, routing hook, model, private terms, and the few messages that name the host's setup |
| `places.py` | The root, inbox and state folder; private writes (folders `0700`, files `0600`, atomic); the index of what was filed or held; the access log; sha256; moves that never overwrite |
| `detect.py` | Sensitive-value detectors and redaction: counts per kind, never values. Checksums (Luhn, ABA, VIN) cut false positives |
| `reader.py` | Type sniffing, `pdftotext`, OCR one page at a time, `.eml` bodies, barcodes; each file read in a forked child under a memory cap and a timeout |
| `fingerprints.py` | sha256 and a text fingerprint (simhash and MinHash) of every file already in the records, cached by path, size and mtime |
| `similar.py` | The near-duplicate fingerprints and the rule that compares them |
| `scan.py` | Screening the inbox: hold secret material, mark duplicates and near duplicates, route, print a table of counts |
| `fields.py` | Profile-driven field extraction: only a profile's allowlisted fields, each value checked against the detectors |
| `llm.py` | An optional pass through a local model (Ollama on `127.0.0.1` only): titles, flags, missing fields; best-effort |
| `dates.py` | The date a document is about (payment, statement or issue date before any other), for its file name |
| `locate.py`, `snapshots.py` | Where each value sits on its page (text-layer or OCR word boxes) and a crop of that spot with every detector region blacked out |
| `tasks.py` | Task files: `fields`, `approve`, `status`, `read`, `keep`, `cleanup` and redacted `extract`s of named pages |
| `approval.py` | The approval: a sha256 seal over the approved content, and the user's confirmation in a desktop dialog (or a typed-back phrase on `/dev/tty`) |
| `preview.py` | The user's preview of a task file: in a terminal, or a self-contained review page (inline CSS and images, no scripts, nothing fetched) with a redacted crop of each value's spot on its page |
| `pairs.py` | Invoice and receipt pairs for one charge, matched by a shared number or the same vendor within 3 days, and whether their totals agree |

## The flow

1. **scan**: every inbox file is screened. Secret material moves to the inbox's `.held/` folder; exact copies of a
   file already in the records are marked, near copies flagged; the host's route hook names a destination.
2. **fields --task PROFILE FILE...**: only the profile's allowlisted fields go into a task file, each value checked
   against the detectors, with where it sits on its page. Nothing outside the profile is ever written.
3. **preview**: the user looks at the task file in their own terminal (the engine refuses when stdout is not a
   terminal).
4. **approve**: the user confirms in a desktop dialog; the approval stores a sha256 over the task's content and
   its sources' sha256s. With no display, `--terminal-confirm` asks for a random phrase typed back on `/dev/tty`.
5. **read --reason**: the assistant reads an approved task file. `read` refuses one changed since approval, one
   whose source document changed, and a superseded one, and logs every read with its reason.

`status` lists task files with counts and no values. `extract --pages` writes a redacted extract of named pages,
only when the user asks for one; it is approved and read the same way.

## The inbox contract

The inbox is one folder that producers write complete document files into and the engine owns from then on:

- only document files: no sidecar, manifest or notes. Dotfiles, dot folders, symlinks and subfolders are ignored;
- a producer writes outside the inbox and renames the finished file in, so a half-written file is never read;
- the engine moves files out of the inbox (to the host's destination, or to `.held/` for secret material) and never
  overwrites a file; it never deletes an original.

The state folder (`.doc-intake/` under the root by default) holds the index, the fingerprint cache, task files,
extracts, review pages and the access log, all `0600`. The fingerprint walk skips the inbox, the state folder and
every dot folder, so a staging folder such as `.inbox.staging/` beside the inbox is never indexed.

## Configuring a host

Example profiles and a fake known-senders map are in `profiles/` (`receipt.json`, `invoice.json`,
`senders.json`); [profiles.md](profiles.md) describes the profile format.

```python
import engine
from engine import scan

engine.configure(
    root=lambda: "/path/to/records",          # read on every call
    inbox=lambda: "inbox",
    profiles_dir="/path/to/profiles",
    senders_file="/path/to/senders.json",     # optional known-senders map
    route=my_route,                            # where a screened document goes (see config.py)
    model=lambda: "",                          # a local model's name, or "" for none
    privacy_terms=lambda: [],                  # words a model-made title must never contain
)
scan.cmd_scan(args)
```

The known-senders map may sit in the profiles folder under any name: the file `senders_file` names is never listed
as a profile, and `fields --task <its name>` is refused. A file named `vendors.json` there is skipped too, for a
host that keeps the map under that name without configuring `senders_file`.

`route(path, ftype, redacted_text, title_fn=None, **opts)` returns a dict with `category`, `profile`, `dest`,
`name`, `note`, `date_from_file` and `name_from_model`. It sees only redacted text. Hosts register their own value
types for their own profiles with `fields.register_type()`.

The review page takes hooks for receipts and invoices: `summary_extras(values)` adds keys to a document's summary,
`money_card(summary)` says where the document goes and what approving does, `money_sections(plan)` adds sections,
`preview_checks(task, name, source, values)` returns warnings and notes (a host's amount or pair checks), and
`preview_intro(n)` words the page's summary line. `vendor_key(name)` sets how pairs compares vendor names. Filing
and any accounting stay in the host.

## Limits

Read these before trusting it:

- **Detectors are pattern-based and US-centric.** SSNs, ABA routing numbers, US phone and address layouts. Names
  are not detected at all. Other layouts, other countries' formats and numbers broken up by OCR errors slip
  through.
- **The local-model pass is best-effort and nondeterministic.** It only adds flags or fills fields that are then
  validated; the detectors stay the primary layer.
- **The approval gate is a speed bump, not a sandbox,** when the assistant runs as the same OS user as the
  engine: a determined local process can edit files or drive a terminal or GUI. Running the assistant as a
  separate user, with access only through a small allowlisted command, is the real boundary.
- **Untrusted input:** every file is read in a child process capped at 1.5 GB of address space and 300 s, with OCR
  at most 25 MP per page. A file past a cap is reported as unscreened, never passed as safe. macOS does not let a
  process lower its address-space limit, so there the child runs without the memory cap and keeps the timeout,
  the OCR pixel cap and ImageMagick's own limits; a runaway read is stopped by the timeout or the OS, not at
  1.5 GB. `scan` says so once a run ("N files read without a memory cap ..."), and `scan --profile-mem` shows
  `mem_cap=yes` or `no` for each file it read. Peak memory (`--profile-mem`) is reported in MB on both platforms.

## Requirements

Python 3 standard library. Optional system tools, each used only when present: `pdftotext`, `pdftoppm` and
`pdfinfo` (poppler), `tesseract`, ImageMagick 7 (`magick`), and the `zxingcpp` Python module with Pillow for
barcodes. Without one, a file it would have read is reported as unscreened or partly read.
