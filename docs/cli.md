# The command line

`python3 -m doc_intake` runs the engine over a config file, so you can use doc-intake without writing a host
program. It is the generic version of the fake host the tests use (`tests/host.py`): the same commands and
arguments, with the records root, profiles and routing read from a TOML file instead of code.

    python3 -m doc_intake [--config PATH] COMMAND [ARGS]

Python 3.11 or newer (for `tomllib`); standard library only.

## Installing

From a checkout, `python3 -m doc_intake` works with nothing installed. Installing adds a `doc-intake` command
that runs the same CLI and exits with the same status:

    pip install .                                                         # from a checkout
    pip install '.[barcodes]'                                             # plus zxing-cpp and Pillow
    pip install git+https://github.com/jaredhoward-dev/doc-intake@v0.1.0  # a pinned tag, once tagged

Only the `doc_intake` package is installed. The example config (`examples/doc-intake.toml`) and the example
profiles (`profiles/`) stay in the repo: copy them next to each other (the example config points at
`../profiles`) or change `profiles_dir` and `senders_file` to where you put them.

Help and the hints the commands print (such as the `preview` and `approve` lines after `fields`) name the form you
ran: `doc-intake` when run as the installed command, `python3 -m doc_intake` when run with `-m`. Each entry point
says which it is (`__main__.py` passes `prog=MODULE` to `cli.main`, whose default is the console script), rather
than guessing from `sys.argv[0]`. The examples below use the `-m` form.

## The config file

Found in this order:

1. `--config PATH`
2. `$DOC_INTAKE_CONFIG`
3. `./doc-intake.toml` in the folder you run the command from

A file named by `--config` or `$DOC_INTAKE_CONFIG` that doesn't exist is an error; the search never falls through
to the next place. A missing or invalid config prints one line naming the problem (and the key), never a traceback,
and exits with status 2.

[`examples/doc-intake.toml`](../examples/doc-intake.toml) is a commented starting point.

| Key | Default | What |
| --- | --- | --- |
| `root` | (required) | The records folder. `~` is expanded; a relative path resolves against the config file's folder. |
| `inbox` | `"inbox"` | The inbox folder's name under `root`. |
| `state` | `".doc-intake"` | The state folder's name under `root` (task files, extracts, the index, the access log). |
| `profiles_dir` | none | The folder of field profiles (`<name>.json`). Needed by `fields`. |
| `senders_file` | none | The known-senders map (JSON). Optional. |
| `model` | `""` | A local Ollama model for `fields --llm`, `scan --llm-names` and `extract --llm-check`; `""` is none. `--model` overrides it. |
| `privacy_terms_file` | none | Words a model-made title must never contain, one per line; blank lines and `#` lines are skipped. Read when needed, never printed. |
| `[[route]]` | none | Routing rules, below. |

`profiles_dir`, `senders_file` and `privacy_terms_file` resolve against the config file's folder when relative, so
a config works the same from any working folder. `inbox` and `state` must stay under `root` (no absolute path, no
`..`). Each path is checked when the config loads.

Unknown keys are an error, at the top level and inside a `[[route]]` table: a typo such as `profile_dir` stops the
command instead of silently keeping a default.

### Routing

`scan` asks where each document would go. The CLI answers with a list of `[[route]]` rules, tried in order against
the document's **redacted** text; the first one that matches wins.

| Rule key | Required | What |
| --- | --- | --- |
| `category` | yes | The document's kind, shown by `scan` (lower-case letters, digits, `_`). |
| `patterns` | yes | Regular expressions (Python `re`), matched case-insensitively. |
| `min_matches` | no | How many of `patterns` must be found (default 1). |
| `profile` | no | The profile `fields --task` should use; it must exist in `profiles_dir`. Without one, `scan` notes that the user should say which fields are needed. |
| `dest` | no | The folder under `root` it would go to. `{year}` is the document's year (or `scan --year`); no other placeholder. |
| `name` | no | The label in the suggested file name, `YYYY-MM-DD - <name><ext>` (default: the category, capitalised). |

A document no rule matches, or every document when there are no rules, gets the engine's default: category
`unknown`, no profile, no destination, and its own file name (or a checked title from the model with
`scan --llm-names`).

TOML literal strings (single quotes) keep backslashes as they are, which suits regular expressions:

```toml
[[route]]
category = "receipt"
patterns = ['\breceipt\b', '\btotal\b|\bamount\s+paid\b']
min_matches = 2
profile = "receipt"
dest = "Receipts/{year}"
```

## Commands

| Command | What |
| --- | --- |
| `scan [--year Y] [--llm-names] [--model M] [--profile-mem] [--retry-skipped]` | Screen every inbox file: type, pages, detector counts (never values), held secret material, exact and near duplicates, routing. |
| `fields --task PROFILE FILE... [--llm] [--model M] [--replaces TASK_JSON] [--labels]` | Write a task file holding only the profile's allowlisted fields. `FILE` is relative to `root`. |
| `preview TASK_JSON [--html] [--no-open]` | The user's preview: in their own terminal, or as a self-contained review page. |
| `approve TASK_JSON\|EXTRACT [--terminal-confirm]` | The user approves, from their own terminal, with a click in a desktop dialog (or a phrase typed back on `/dev/tty` with `--terminal-confirm`). |
| `status [--detail]` | Task files with approval state and counts; field names, never values. |
| `read TASK_JSON\|EXTRACT --reason WHY [--fields a,b]` | Read an approved task file or extract. Refused before approval, after any change, or without a reason. Every read is logged. |
| `keep TASK_JSON\|EXTRACT` | Keep a task file or extract past `cleanup`. |
| `wrong TASK_JSON --doc N --field [GROUP.]FIELD [--plan P] [--row N] [--reason WHY]` | The user, from their own terminal, marks one value wrong (the review page shows the command beside each value). That document is read again with the value's spot left out; the result is a new unapproved task to preview and approve again. |
| `extract FILE... --pages SPEC [--llm-check] [--model M]` | A redacted extract of named pages, approved and read like a task file. |
| `cleanup [--task PROFILE] [--older-than DAYS] [--dry-run]` | Delete old task files and extracts (30 days by default). |
| `pairs [TASK_JSON...]` | Invoice and receipt pairs among approved task files, and whether their totals agree. Exit 1 on a mismatch. |
| `doctor [--no-model]` | Check the setup without reading any document: the config, the places it names, the optional tools, the memory cap and the local model. Below. |

The CLI adds no way around a guard. `approve` refuses anywhere but the user's own terminal and then still needs the
desktop click; an assistant never runs it. `read` needs an approval and a reason. Nothing the CLI prints holds
document text; values reach the assistant only through `read` of an approved file. See
[engine.md](engine.md) for the full flow and its limits.

## Checking the setup: `doctor`

`doctor` says what the other commands would find, without reading a document:

- the Python and doc-intake versions;
- the config file it would use and how it was found (`--config`, `$DOC_INTAKE_CONFIG` or `./doc-intake.toml`),
  whether it loads, and whether the root, inbox, state folder, `profiles_dir` and `senders_file` exist, with the
  profile names found (names only; `privacy_terms_file` is reported as configured or not, never read);
- the optional tools: `pdftotext`, `pdftoppm`, `tesseract`, ImageMagick (7, 6 or missing), `zxingcpp` and Pillow;
- whether the memory cap applies here, from one bounded run of a function that reads nothing (on macOS it
  doesn't; see the end of this page);
- the local model: its name, or `not configured`. Only when one is configured does it ask the model server on
  127.0.0.1 whether it answers (a 3-second timeout, nothing beyond 127.0.0.1); `--no-model` skips that.

It prints one `name: result` line each (`ok`, `missing`, `not configured`), counts and names only. It is
read-only: it creates no folder (a missing inbox or state folder is reported, not made, since the commands that
need them make them) and writes nothing to the access log or the state folder. It exits 0 when the config loads
and the root exists, whatever optional tools are missing, and 2 on a config error or a missing root, with the same
one-line message the other commands give; the tool lines are still printed then.

```console
$ python3 -m doc_intake doctor
python: 3.12.3 ok
doc-intake: 0.1.0
config: /home/example/doc-intake.toml (found by ./doc-intake.toml)
config loads: ok
root: ok
inbox: ok
state folder: missing
profiles_dir: ok (2 profile(s): invoice, receipt)
senders_file: ok
privacy_terms_file: not configured
routing rules: 3
pdftotext: ok
pdftoppm: ok
tesseract: missing
imagemagick: ok (6)
zxing: missing
pillow: ok
memory cap: ok
model: example-model
model server: not reachable on 127.0.0.1:11434
```

## An example session

With `doc-intake.toml` in the current folder and one receipt in the inbox (fake data):

```console
$ python3 -m doc_intake scan
#  file      type  pages  sensitive  destination      suggested name
1  shop.txt  text  1      -          Receipts/2030    2030-03-03 - Receipt.txt

$ python3 -m doc_intake fields --task receipt inbox/shop.txt
...
The user previews it in their own terminal: python3 -m doc_intake preview "<task>.json"
then, once they have looked: python3 -m doc_intake approve "<task>.json"

$ python3 -m doc_intake read "<task>.json" --reason "monthly expenses"
doc-intake: <task>.json is not approved: the user previews it, then approves it
```

The user then runs, in their own terminal:

```console
$ python3 -m doc_intake preview "<task>.json"
$ python3 -m doc_intake approve "<task>.json"
approved: <task>.json (1 documents, N fields)
```

and only then does `read` return the approved fields as JSON, logging the read and its reason in
`<state>/access.log`.

On macOS a bounded read runs without the memory cap (macOS refuses it); the timeout, the OCR pixel cap
and ImageMagick's own limits still apply. `scan`, `fields`, `extract` and `preview --html` each say
once per run how many files they read without the cap, and `scan --profile-mem` shows `mem_cap=yes`,
`no` or `-` (not read in a bounded child) per file. `doctor` says up front whether the cap applies.
See [engine.md](engine.md).
