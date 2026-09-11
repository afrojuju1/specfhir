# SpecFHIR

- Follow PLAN.md. Implement only the requested phase or milestone; no custom FHIR semantics.
- Use uv for Python commands. Check with `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest`.
- Integration tests require the local Compose database and run with
  `SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir uv run pytest`.
  Tests use an isolated schema, never replace the application's dataset.
- The default test run is serial. Bounded parallel runs use `uv run pytest -n 4`;
  preserve the configured database and validator xdist groups and four-worker cap.
  Installed acceptance and real smokes remain explicit opt-ins documented in README.
- Never run sync/build concurrently with tests.
- Package archives, database files, caches, and credentials stay out of Git.
- No commits or pushes unless explicitly requested.

## Issue tracking

- Use Beads (`bd`) for project issues; IDs use the `sf` prefix.
- Run `bd prime` for workflow context and `bd ready` for available work.
- Keep issues under the relevant milestone/epic. Git commits and pushes still
  require explicit user authorization.
