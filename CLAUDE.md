# doc-intake: rules for Claude

doc-intake exists so an assistant can work with documents without seeing them. These rules apply to
working on this repo and to using it.

- **Never read a real document.** No `pdftotext`, `tesseract`, `cat` or the Read tool on a user's
  documents, inbox or state folder. The assistant reads only approved fields, through the tool.
- **Never approve.** Approval is the user's click in a desktop dialog. Never run `approve` for the
  user, and never use anything that could drive a terminal or a GUI to fake it.
- **Fixtures are synthetic.** Fake names, vendors and amounts; numbers that only pass checksums
  (test card numbers, example routing numbers). No real path, name or value in any commit.
- **Stdlib only** in `doc_intake/` (plus optional `zxingcpp` and `PIL`, imported inside functions).
  `tests/test_generic.py` enforces it.
- **No host specifics in the engine**: no folder layout, vendor list, bookkeeping or locale rules.
  A host passes those in through `configure()`.
- Tests: `python3 -I -m pytest -q tests`. Prefer targeted tests locally; CI runs
  the suite.
- Work on a branch in a worktree beside the main checkout, and open a pull request; `main` changes
  only by merge.
