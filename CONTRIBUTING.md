# Contributing

- Open an issue first for anything larger than a small fix.
- Fixtures are synthetic. A pull request with a real document, path, name or value is closed.
- `doc_intake/` stays stdlib-only and host-agnostic (see `CLAUDE.md`).
- Tests: `python3 -I -m pytest -q tests`, and a test that fails without your
  change.
- Pull request titles follow Conventional Commits (`feat:`, `fix:`, `docs:`, ...).
