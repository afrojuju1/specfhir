# SpecFHIR

Local, version-aware FHIR evidence for agents: exact lookup, profile inspection,
source-backed search, and delegated HL7 validation through a shared Python API,
CLI, and four MCP tools. Retrieval works offline after sync. SpecFHIR returns
published evidence, not generated clinical answers.

[PLAN.md](PLAN.md) defines the current design and scope.
[VALIDATION.md](VALIDATION.md) records dated acceptance and performance evidence.
[Fixture documentation](tests/fixtures/README.md) explains the shared test inputs.

## Run

Requires Python 3.13, uv, and Docker Compose. Java 21 is in the validator image;
no host Java installation is needed.

```bash
docker compose up -d --wait postgres
uv sync --locked
uv run specfhir sync --with-validator --json
uv run specfhir check --with-validator --json
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --view differential --json
uv run specfhir search "patient identifier requirements" --mode hybrid --json
uv run specfhir validate patient.json --profile USCorePatient --json
```

The first sync downloads pinned packages and model files, indexes the dataset,
then prepares and starts the validator. Subsequent unchanged runs reuse both.
`patient.json` is your input file; submitted instances are never indexed.

[specfhir.toml](specfhir.toml) selects exact package roots and publications;
[specfhir.lock](specfhir.lock) pins their dependency graph, checksums, sources,
and embedding runtime. [uv.lock](uv.lock) pins Python dependencies.
Current configured roots are R4 4.0.1, US Core 9.0.0 (default), PAS 2.0.1/2.1.0,
CRD 2.1.0/2.2.1, CDEX 2.0.0/2.1.0, and DTR 2.1.0/2.2.0.
Additional dependency versions remain distinct in the lock.

Compose binds PostgreSQL to `127.0.0.1:55432` and the validator to
`127.0.0.1:55433`. PostgreSQL uses user/database `specfhir` and local development
trust authentication. Its files live under ignored `.specfhir/postgres`.
For a different database port, set both Compose's `SPECFHIR_PORT` and the client's
`SPECFHIR_DSN`. Use a dedicated database per dataset.

Commands accept `--config /absolute/path/custom.toml`; the lock and `.specfhir`
cache follow that file's parent directory. `SPECFHIR_DSN` overrides the database
connection independently of the configuration filename.

## Lookup and search

```bash
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --view raw --json
uv run specfhir resolve PASClaim --package 'hl7.fhir.us.davinci-pas#2.1.0' --json
uv run specfhir search "patient identifier requirements" --mode lexical --limit 5 --json
```

Queries use the selected package and its exact locked dependency closure.
Matches in the selected package take precedence. Dependencies may contain multiple
versions; unresolved collisions return candidates rather than choosing the newest.
Use `--package` for an explicit context and `--artifact-version` for a business
version where supported; see each command's `--help`.

`resolve` matches canonical URLs, IDs, and names exactly. Names ending in `Profile`
also have a collision-aware suffix-free alias. `USCorePatient.identifier` is an
element selector, not FHIRPath. Slices and choice elements retain their literal IDs.
Effective inspection uses the published snapshot. Differential-only or malformed
projections report unavailable; `--view differential` and `--view raw` provide
explicit alternatives. No snapshot synthesis or automatic repair occurs.

Search modes:

- `auto`: hybrid if the published dataset has embeddings, otherwise lexical.
- `lexical`: PostgreSQL FTS and fuzzy name/title matching, without inference.
- `semantic`: exact cosine distance within the selected context.
- `hybrid`: reciprocal rank fusion of up to 100 lexical and 100 semantic candidates.

The configured model is quantized BGE small, 384 dimensions, with a 256-token
budget. Only sync downloads model files. Queries use verified files from the
published model pin; an unavailable model is an explicit error, with lexical mode
still usable. Generated narratives and copyright remain lexical-only.
Search accepts up to 500 characters and returns 1–50 results with bounded excerpts,
source locations, and explicit truncation. Use exact lookup for identifiers.

## Expand or rebuild

```bash
uv run specfhir packages versions hl7.fhir.us.davinci-pas --json
uv run specfhir packages pages 'hl7.fhir.us.davinci-pas#2.1.0' --json
uv run specfhir packages list --json
```

`versions` queries the registry without changing pins. `pages` previews candidates
from the locked IG's page metadata. `list` reports installed coverage, exclusions,
reference findings, and coherence with the configuration and lock.

To add a release, edit the exact roots and publication entries in `specfhir.toml`,
then intentionally update the lock:

```bash
uv run specfhir sync --update-lock --with-validator --json
uv run specfhir check --with-validator --json
uv run pytest tests/acceptance --live-acceptance -q
```

Documentation discovery follows `ImplementationGuide.definition.page`. Prefer the
exact release's `full-ig.zip`: its embedded package archive must match the package
pin. The ZIP, selected page bytes, member paths, and public URLs are pinned.
Generated resource renderings, administrative pages, and external links are not
blindly crawled. Explicit document URLs remain available for publications without
an archive; see the configuration models in [config.py](src/specfhir/config.py).

Preparation is incremental per package/document/model identity. Existing verified
extraction and embedded-passage caches are reused; only changed inputs need work.
**Database publication still replaces the complete dataset atomically.** Readers
see the old or new dataset, never a partial publication. An unchanged sync verifies
cached inputs and skips preparation/publication.

```bash
uv run specfhir sync --rebuild --with-validator --json
uv run specfhir sync --prune-cache --with-validator --json
```

`--rebuild` forces full publication. `--prune-cache` removes recognized obsolete
prepared/embedded/vector cache generations only after successful sync. It retains
current generations, unknown files, symlinks, source archives, publications,
model downloads, and validator snapshots. These caches are disposable, not Git inputs.

If indexing fails, the previous published dataset survives; a changed lock may
still describe pending inputs, which inventory reports as a mismatch. Validator
refresh follows database publication, so a failed refresh can leave a new index
with an old service. Rerun `sync --with-validator` to finish that step.

## Validator operations

The persistent Java adapter delegates rules to pinned HL7 Validator 6.10.4.
It reuses two engines, prewarms the default context, and permits one active
validation plus eight waiting requests. Excess admission returns 429; queue waits
are bounded to ten seconds. A 150-second execution watchdog halts a stuck JVM;
Compose restarts it. Failures are returned without automatic request retries.

Normal builds use `sync --with-validator`. For validator source/image changes,
explicitly prepare and recreate the service:

```bash
uv run specfhir validator-setup --json
docker compose up -d --build --force-recreate --wait validator
```

The service snapshot tracks exact package/dependency and validator support pins,
not retrieval pages or embedding settings. A matching healthy service is reused.
Only the selected locked closure is loaded. Instance-declared profiles cannot
install extra packages; both service and client verify loaded-package evidence.

Without `--profile`, validation requests base R4; HL7 also checks `meta.profile`.
An explicit profile resolves in the same package context as retrieval. SpecFHIR
supplies no external instance repository. Missing references and incomplete
terminology coverage remain visible in the outcome.

Results separate execution, findings, and coverage. `status: "ok"` means the
operation completed, and may accompany validation errors. Inspect
`data.findings.errors`; zero errors do not establish complete conformance.
Inputs are JSON, at most 10 MiB. Returned issues are capped at 500, with full counts
and truncation reported. Read validator output is bounded to 16 MiB.

The container uses a 2 GiB heap within a 4 GiB limit. Package indexes/terminology
cache use a private Docker volume, keyed by snapshot; temporary files use a 64 MiB
tmpfs. Instances, outcomes, and request logs are not persisted by the service.
Returned findings can contain input values.

Offline is the default: HL7 prohibits network access, but Docker bridge networking
is not OS-enforced egress isolation. Optional online terminology uses a separate
process because HL7's network policy is global:

```toml
[validator]
timeout_seconds = 180
service_url = "http://127.0.0.1:55433"
online_service_url = "http://127.0.0.1:55434"
terminology_endpoint = "https://tx.fhir.org/r4"
```

```bash
SPECFHIR_TERMINOLOGY_ENDPOINT=https://tx.fhir.org/r4 \
  docker compose --profile online up -d --build --wait validator-online
uv run specfhir validate patient.json --profile USCorePatient --terminology-mode online --json
```

The endpoint must be explicitly configured and HTTPS. Recreate the online service
separately after snapshot changes. Offline acceptance does not verify online
terminology interoperability.

For build batches, use `validate-cases cases.json --json` with a manifest such as:

```json
{"cases":[{"name":"patient","instance":"patient.json","package":"hl7.fhir.us.core#9.0.0","profile":"USCorePatient"}]}
```

Instance paths are relative to the manifest. Names must be unique; package and
profile are explicit. CLI exit codes: 0 success/warnings, 1 execution or input
failure, 2 not found/unavailable effective definition, 3 ambiguous, 4 validation
errors. Batch execution failures take precedence over error findings.

## MCP

The official Python SDK serves `resolve`, `inspect`, `search`, and `validate`
over stdio with the same structured results as the CLI. Logs use stderr.
Validation accepts JSON content, not a client filesystem path. Sync is an explicit
CLI operation; MCP has no sync tool or implicit package downloads.

Example client configuration; adjust the checkout path:

```json
{
  "mcpServers": {
    "specfhir": {
      "command": "uv",
      "args": [
        "--directory", "/Users/adeb/Projects/specfhir",
        "run", "--no-sync", "specfhir", "mcp",
        "--config", "/Users/adeb/Projects/specfhir/specfhir.toml"
      ]
    }
  }
}
```

Complete setup before connecting. Pass `SPECFHIR_DSN` in the client environment
if using a non-default database.

## Verify

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Database integration tests use isolated schemas with `SPECFHIR_TEST_DSN`; they
never replace the application dataset. Real smokes and installed acceptance are
explicit opt-ins:

```bash
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir \
SPECFHIR_REAL_SMOKE=1 SPECFHIR_VALIDATOR_SMOKE=1 \
uv run pytest --live-acceptance -q -o junit_family=legacy \
  --junitxml=.specfhir/acceptance.xml
```

Do not run sync/build concurrently with this suite: database advisory locks span
schemas. Acceptance reuses reviewed fixtures and compares API, CLI, and actual
MCP results. Published archive examples retain full outcomes in JUnit and assert
reviewed error categories/counts. Updating those baselines requires reviewing the
new outcome, not blindly accepting changed counts.

`specfhir check` is read-only readiness, provenance, and release-scope coverage;
`--with-validator` adds service readiness, not instance validation. Pytest owns
behavioral acceptance. No second check scripting framework is required.

Existing benchmarks:

```bash
uv run python scripts/benchmark_search.py
uv run python scripts/benchmark_search.py --build
uv run python scripts/benchmark_validator.py --restart-check
```

The default search benchmark is read-only: 19 prose queries and eight exact
lookups. `--build` also measures an uncached 512-passage inference sample and
performs a full rebuild. The validator lifecycle benchmark intentionally exercises
admission and eviction; `--restart-check` interrupts/restarts the service. Run
these separately from other validation or sync work. Dated results and known
retrieval misses are in [VALIDATION.md](VALIDATION.md).
