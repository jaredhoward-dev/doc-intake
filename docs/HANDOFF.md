# Handoff: continuing the work

Where things stand and how to pick the work up on another machine.

## State

- Stage 1 (the engine as `doc_intake/`, CI, docs) is on `main`.
- Stage 2 is done: the engine's tests against a fake host (`tests/host.py`), example profiles (`profiles/`,
  [profiles.md](profiles.md)) and a minimal CLI ([cli.md](cli.md)), tagged `v0.1.0`.
- Stage 3 is under way: `wrong`, `doctor`, `box_row`, a utility-bill example; see [ROADMAP.md](../ROADMAP.md).
  They are on `main` but not yet in a tag.
- The engine came from its first host; changes there after the copy must be carried over by hand
  until the host pins this package instead. Fixes made here (bounded reads on macOS, the senders map
  never listed as a profile) need carrying back the same way.

## Continue

    git clone https://github.com/jaredhoward-dev/doc-intake.git
    cd doc-intake
    git fetch origin && git worktree add ../doc-intake-<slug> -b <branch> origin/main
    cd ../doc-intake-<slug>
    python3 -I -m pytest -q tests

Next: the stage 3 items left in [ROADMAP.md](../ROADMAP.md) and the follow-ups in
[followups.md](followups.md). Each is its own pull request against `main`.

## Rules that travel with the code

- Fixtures are synthetic: fake names, fake numbers that pass the checksums (test cards, the
  example routing numbers), no real document ever.
- The engine imports only the standard library, itself, and the optional `zxingcpp` and `PIL`.
- Never print a document's text in tests or logs; counts and field names only.
