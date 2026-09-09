# SpecFHIR

Local, source-backed FHIR knowledge for agents. **Phase 1 is implemented:** package
sync, exact resolution, and snapshot/differential/raw inspection. Search, MCP,
embeddings, and validation remain planned in [PLAN.md](PLAN.md).

## Run

Requires uv and Docker with Compose. Python 3.13 is selected by .python-version.

```bash
docker compose up -d --wait
uv run specfhir sync
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --json
```

The local development database listens on `127.0.0.1:55432`, using user/database
`specfhir` and trust authentication. This Compose configuration is for a trusted
local development machine, not a shared or production database. PostgreSQL data
is stored under `.specfhir/postgres/`. To use a different port, set SPECFHIR_PORT
for Compose and SPECFHIR_DSN for the application:

```bash
SPECFHIR_PORT=55433 docker compose up -d --wait
SPECFHIR_DSN=postgresql://specfhir@localhost:55433/specfhir uv run specfhir sync
```

Use `--config /path/to/specfhir.toml` on each command when outside the project.
The package cache and lock location follow that configuration file; the database
connection comes from SPECFHIR_DSN. Use a dedicated database per project: sync
replaces the configured dataset within that database.

## Exact lookup and inspection

`USCorePatient` is the suffix-free alias of the published `USCorePatientProfile`
name. Aliases use exact matching and collision detection, not fuzzy search.
`--package` selects an exact package version; its own matches take precedence over
its dependencies. Otherwise all matches in its locked dependency closure are
considered, and duplicate definitions return ambiguity.

```bash
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir resolve Patient.id --package 'hl7.fhir.r4.core#4.0.1' --json
uv run specfhir resolve USCorePatient.extension:race --json
uv run specfhir resolve \
  'http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient|9.0.0' \
  --element Patient.identifier --json
uv run specfhir inspect USCorePatient --view differential --json
uv run specfhir inspect USCorePatient --view raw --json
```

The identifier example returns cardinality `1..*`, `mustSupport: true`, package
`hl7.fhir.us.core#9.0.0`, and pointer `/snapshot/element/14` into the original
package file. Must-support is reported separately from cardinality.

Element paths matching multiple slices return candidates. Select a slice by its
ElementDefinition.id. Choice names such as `value[x]` remain literal FHIR element
paths; expressions and concrete-choice expansion are not evaluated.

Default inspection is compact and limits the element list to 100, with an
explicit `truncated` flag. Select an individual element for detailed constraints,
bindings, types, and guidance. Raw inspection explicitly returns the entire stored
artifact. Missing/malformed effective definitions are never replaced with a
partial differential or repaired automatically.

Python callers use `specfhir.search.resolve()` and `specfhir.search.inspect()`;
they return the same Pydantic Result serialized by the CLI. They accept
`package`, `artifact_version`, `element`, and `config_path`; inspect also accepts
`view="snapshot"`, `"differential"`, or `"raw"`. Acquisition/configuration failures
raise actionable exceptions; the CLI converts them to error results.

Exit codes: 0 success; 1 execution/configuration failure; 2 not found or effective
definition unavailable; 3 ambiguous. JSON output is written to stdout.

## Reproducibility and actual coverage

specfhir.toml selects roots. specfhir.lock records exact package versions,
dependency edges, source URLs, and SHA-256 archive hashes. The committed project
files should include both lockfiles; runtime archives and database state are
ignored. First sync creates a missing package lock. Subsequent sync honors it;
use `sync --update-lock` after changing roots. Only exact dependency versions are
supported. Version ranges and cycles fail explicitly.

The checked R4 4.0.1 / US Core 9.0.0 graph contains **18 packages**. Phase 1 indexes
**25,033 artifacts and 90,048 elements** from compatible content:

- All exact dependency versions are retained; there is no version unification.
- Three R5 dependencies are inventoried but excluded from R4 retrieval.
- The R4 examples package is inventoried but instance content is excluded.
- Sixteen artifacts contain malformed snapshot/differential representations.
  Raw JSON remains available; affected element projections are unavailable.
- Only supported top-level package resource files are indexed. Other resource
  types, nested example files, and resource-level incompatible releases are counted
  as skipped. `sync --json` includes inventory, reasons, and skip counts.

These limitations mean the database is an attributable R4 knowledge subset, not a
claim of complete dependency coverage or FHIR conformance validation.

Package archives are read without filesystem extraction. Size/member limits,
manifest identities, archive checksums, and element IDs are checked. Files are
prepared before publication. PostgreSQL COPY loads artifacts/elements within one
transaction; an advisory lock serializes sync. Readers use a consistent database
snapshot, and failed publication preserves the previous successful dataset.
A pending lock update can survive failed publication; the database retains its
own published lock digest. Repeating an unchanged sync verifies cached package
hashes and returns `unchanged`.

No LLM calls are made. After a successful sync, retrieval is offline. Resync is
also offline if all locked archives are cached. A clean rebuild requires network
access to fetch the locked archives:

```bash
docker compose down
rm -rf .specfhir
docker compose up -d --wait
uv run specfhir sync
```

This deletes only generated state for this project. Keep specfhir.toml,
specfhir.lock, compose.yaml, and uv.lock. Schema changes during this early phase
use an explicit rebuild; there is no migration framework.

## Verify

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir uv run pytest -q
```

Database tests use isolated schemas and remove them on completion. Without
SPECFHIR_TEST_DSN, PostgreSQL tests are visibly skipped. For the real locked
R4/US Core ingestion, source lookup, no-op sync, and rebuild test, first run the
normal sync to populate the archive cache, then:

```bash
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir \
  SPECFHIR_REAL_SMOKE=1 uv run pytest -q
```

The real-package test blocks HTTP, reuses verified cached archives, and builds
its own temporary index twice. It does not replace the application's dataset.
