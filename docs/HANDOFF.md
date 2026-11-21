# Handoff: continuing stage 1

Where things stand and how to pick the work up on another machine.

## State

- `main`: README and license only.
- The stage-1 pull request (draft): the engine as `doc_intake/`, CI, tests, these docs.
- The engine came from its first host at that host's latest main; changes there after the copy
  must be carried over by hand until the host pins this package instead.

## Continue

    git clone https://github.com/jaredhoward-dev/doc-intake.git
    cd doc-intake
    git fetch origin && git worktree add ../doc-intake-stage-1 stage-1
    cd ../doc-intake-stage-1
    python3 -I -m pytest -q tests

Then the next open issue, in order: port the tests against a fake host, example profiles, the
CLI, packaging. Each is its own pull request against `stage-1` or, once stage 1 merges, `main`.

## Rules that travel with the code

- Fixtures are synthetic: fake names, fake numbers that pass the checksums (test cards, the
  example routing numbers), no real document ever.
- The engine imports only the standard library, itself, and the optional `zxingcpp` and `PIL`.
- Never print a document's text in tests or logs; counts and field names only.
