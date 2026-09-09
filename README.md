# SpecFHIR

Local, source-backed FHIR knowledge for agents. **Phases 1–4 are implemented:** package
sync, exact resolution, snapshot/differential/raw inspection, lexical/hybrid search,
delegated HL7 instance validation, and local MCP. Scope is recorded in [PLAN.md](PLAN.md).

## Run

Requires uv and Docker with Compose. Python 3.13 is selected by .python-version.

```bash
docker compose up -d --wait postgres
uv run specfhir sync
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --json
uv run specfhir search "patient identifier requirements" --json
```

The local development database listens on `127.0.0.1:55432`, using user/database
`specfhir` and trust authentication. This Compose configuration is for a trusted
local development machine, not a shared or production database. PostgreSQL data
is stored under `.specfhir/postgres/`. To use a different port, set SPECFHIR_PORT
for Compose and SPECFHIR_DSN for the application:

```bash
SPECFHIR_PORT=55435 docker compose up -d --wait postgres
SPECFHIR_DSN=postgresql://specfhir@localhost:55435/specfhir uv run specfhir sync
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
definition unavailable; 3 ambiguous; 4 validation completed with errors/fatal findings.
Warnings alone do not change exit code 0. JSON output is written to stdout.

## Reproducibility and actual coverage

specfhir.toml selects roots. specfhir.lock records exact package versions,
dependency edges, source URLs, and SHA-256 archive hashes. The committed project
files should include both lockfiles; runtime archives and database state are
ignored. First sync creates a missing package lock. Subsequent sync honors it;
use `sync --update-lock` after changing roots. Only exact dependency versions are
supported. Version ranges and cycles fail explicitly.

The initial R4 4.0.1 / US Core 9.0.0 checkpoint contained **18 packages**,
**25,033 artifacts and 90,048 elements**. Its lexical-only extraction produced
121,738 passages; enabling embeddings splits eligible passages further to respect
the tokenizer limit. `sync --json` reports the published passage/vector counts:

- Exact non-core dependency versions are retained. The approved Backport core exception
  is recorded explicitly; there is no general version unification.
- Three R5 dependencies are inventoried but excluded from R4 retrieval.
- Subscriptions Backport 1.1.0 declares FHIR 4.0.0 and is excluded from retrieval;
  HL7 loads it for validation with the explicitly selected core 4.0.1. This distinction
  is retained in the inventory and validation coverage report.
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
docker compose up -d --wait postgres
uv run specfhir sync
```

This deletes only generated state for this project. Keep specfhir.toml,
specfhir.lock, compose.yaml, and uv.lock. Phase 2 adds its document table/indexes and automatically rebuilds on the next
sync, preserving the existing dataset until publication. Search reports unavailable
until that sync succeeds. Future incompatible schema changes require an explicit
rebuild; there is no migration framework.

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


## Search (Phase 2)

```bash
uv run specfhir search "patient identifier requirements" --limit 5 --json
uv run specfhir search "patient language binding" --json
uv run specfhir search "administrative gender" \
  --package 'hl7.fhir.r4.core#4.0.1' --resource-type ValueSet --json
```

Search indexes package descriptions, purposes, copyright text, plain-text
narratives, and element definitions/comments/requirements/bindings/slicing rules.
It uses a supplied valid snapshot when available, otherwise a valid differential;
each element passage labels its representation. It does not reconstruct inherited
FHIR semantics. Narrative scripts/styles are discarded without execution. Passages
retain an artifact JSON pointer, optional element ID, and zero-based chunk number.

Text is split at paragraph/word boundaries into at most 4,000 characters. Results
contain at most 1,000 characters per passage and explicitly mark truncated excerpts.
Embedding-enabled sync applies the additional token bounds described below.
Names, titles, and element IDs receive higher FTS weight than body text.

[PostgreSQL FTS](https://www.postgresql.org/docs/17/textsearch-controls.html) uses
English stemming and web-search syntax (quoted phrases, OR, and negation).
[pg_trgm](https://www.postgresql.org/docs/17/pgtrgm.html) adds typo-tolerant
name/title candidates. Matching results owned by the selected package precede
matches from its dependencies; within those groups, FTS rank/fuzzy similarity and
stable source tie-breakers determine order. Scores are ranking signals, not
confidence percentages. Duplicate evidence is collapsed while returning the
selected source's provenance. Missing canonical URLs use resource type/id identity.

With `--mode lexical`, results label each passage's `method: fts` or `fuzzy`, and list
excluded packages in the selected dependency closure. Empty results are a successful
search with an empty list; invalid filters/queries are explicit errors. Limits are
1–50 and queries are capped at 500 characters. Use `resolve` for exact identifiers;
search does not silently resolve ambiguity or provide generated answers.

The reviewed cases in [tests/fixtures/retrieval/search_queries.json](tests/fixtures/retrieval/search_queries.json) cover
patient identifiers, language bindings, race slices, interpreter guidance, and
administrative-gender terminology. The real-package smoke test requires each
expected source within the top five and verifies its JSON pointer exists in the
stored original artifact. The Phase 2 lexical baseline ranked the identifier requirement second.

## Local MCP (Phase 2)

```bash
uv run --no-sync specfhir mcp --config /Users/adeb/Projects/specfhir/specfhir.toml
```

The [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
serves four stdio tools: `resolve`, `inspect`, `search`, and `validate`. It handles
protocol framing and structured tool results; the tools share the Python API and
serialization/error boundary with the CLI. Logs use stderr. There is no HTTP
listener, sync tool, or implicit package download. Validation accepts JSON content,
never a client-supplied filesystem path; online terminology requires an explicit mode.

Example client configuration (adjust the checkout path on another machine):

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

Run `uv sync` and `specfhir sync` before connecting. Pass SPECFHIR_DSN in the client
process environment if using a non-default database. The integration tests launch
an actual stdio subprocess, list its tools, and compare its structured success,
not-found, and invalid-input results with the CLI.


## Local embeddings and hybrid retrieval (Phase 3)

The supplied configuration enables semantic indexing. Start the updated Compose
service once to use the pinned pgvector/PostgreSQL 17 **Trixie** image (the same
collation-library family as the previous PostgreSQL image), then sync:

```bash
docker compose up -d --wait postgres
uv run specfhir sync
uv run specfhir search "Where can I put a person's medical record number?" --mode hybrid --json
uv run specfhir search "patient identifier requirements" --mode lexical --json
```

The Python API and MCP search tool accept the same `mode` parameter:

- `auto` (default): hybrid when the **published index** has embeddings, lexical otherwise.
- `lexical`: FTS and fuzzy matching; never loads model files or runs inference.
- `semantic`: exact cosine-nearest matches within the selected package closure/filter.
- `hybrid`: equal-weight reciprocal rank fusion, `sum(1 / (60 + rank))`, over up to
  100 lexical and 100 semantic candidates, deduplicated by source identity and text.

Both candidate lists prioritize the selected package before its dependencies.
Fusion uses ranks rather than adding incomparable FTS/cosine scores. No approximate
vector index, reranker, LLM query rewriting, or external embedding service is used.
Exact `resolve` and `inspect` continue through the structured lookup path.

```toml
[embedding]
enabled = true
model = "BAAI/bge-small-en-v1.5"
revision = "52398278842ec682c6f32300af41344b1c0b0bb2"
max_tokens = 256
```

Phase 3 supports this 384-dimensional model family using the quantized ONNX files
from [Qdrant's model repository](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q)
through [FastEmbed](https://qdrant.github.io/fastembed/). Model revision, six artifact
checksums, dimensions, token budget, extraction format, semantic exclusions, and
FastEmbed/ONNX/tokenizer versions are pinned in specfhir.lock and recorded in the
published index metadata. Changing pinned settings or inference libraries requires
`sync --update-lock` and forces rebuilding. Unsupported models fail explicitly.

Each embedding includes up to 48 heading tokens plus the source passage. Splitting
uses the pinned tokenizer's original character offsets and verifies re-encoded
lengths, including special tokens. Inputs exceeding the configured budget fail;
they are never silently truncated. `max_tokens` supports 128–512. A source locator
and chunk number remain attached to each resulting passage.

Descriptions, purposes, element guidance, and non-generated narratives receive
vectors. Copyright text and publisher-marked **generated** narratives remain
lexical-only, avoiding embeddings of generated terminology tables and boilerplate.
This is explicit coverage, not a claim that every document has a vector.

Only sync may download model files. Semantic queries use verified local files from
the published pin, even if the configuration was subsequently edited. Missing or
changed model files and incompatible runtimes produce explicit errors; automatic
mode does not silently downgrade an already semantic index. Select lexical mode
when model inference is unavailable. An already-loaded model remains usable in
its current process; a fresh process revalidates its files.

A disposable SQLite cache under `.specfhir/models/` stores completed vectors keyed
by the full model pin and exact input hash. It allows repeated inputs and retries
to reuse completed inference. Document rows and vectors are still published in one
PostgreSQL transaction. Download/model/inference failure leaves the prior published
dataset usable. Disabling embeddings and syncing publishes a lexical-only index.

Run the reproducible comparison after a semantic sync:

```bash
uv run python scripts/benchmark_search.py
```

It compares the five reviewed lexical cases plus three paraphrases in
[tests/fixtures/retrieval/semantic_queries.json](tests/fixtures/retrieval/semantic_queries.json), reporting expected-source
rank in the top five, median of three warm requests, preparation time, database
size, and model/cache disk usage. These are a small acceptance set, not a general
FHIR retrieval-quality benchmark. Initial indexing is CPU-bound; the tested local
setting uses eight ONNX threads and length-grouped batches of 64.

Measured results and known retrieval misses are recorded in [PHASE3_VALIDATION.md](PHASE3_VALIDATION.md).
The Phase 3 checkpoint contained **130,444 passages and 101,389 vectors**.
Current PAS coverage and acceptance results are recorded in [PAS_VALIDATION.md](PAS_VALIDATION.md).


## Delegated validation (Phase 4)

Prepare the locked package snapshot, then start the persistent validator. Java 21
is included in its Docker image; no host Java installation is required.

```bash
uv run specfhir validator-setup --json
docker compose up -d --build --force-recreate --wait validator
uv run specfhir validate patient.json --profile USCorePatient --json
```

Docker publishes the Java service directly at `http://127.0.0.1:55433`, with readiness
at `/health`. Setup/build may download pinned dependencies; validation does not
provision packages. After changing the retrieval lock, repeat setup and recreate the
service. Requests against an older snapshot fail explicitly.

The shared Python operation is `specfhir.validator.validate(instance, package=...,
profile=..., terminology_mode="offline", config_path=...)`. It returns the same
Result used by CLI and MCP. An explicit profile resolves through SpecFHIR's locked
package context and is passed as canonical URL plus artifact version. Without
`--profile`, the request is base R4 validation; HL7 still checks profiles declared
in `meta.profile`. Declared profiles must resolve in the selected retrieval context.
No profile is inferred from the default package.

The result separates `execution` (completed/failed), `findings` (severity counts),
and `coverage` (limited/unknown). An invalid instance can have `status: "ok"` because
the validator completed; inspect `findings.errors` or CLI exit code 4. Failures keep
findings unavailable. Returned issues retain HL7 severity, code, details, diagnostics,
location/expression, and HL7 issue extensions. Input references are listed in context;
`-check-references` enables HL7 reference checks. Unresolved findings appear in issues;
SpecFHIR supplies no external instance repository. Coverage stays limited even when
there are no errors. Results cap issues at 500, expose truncation and full counts,
and accept JSON instances up to 10 MiB; validator output is capped at 16 MiB when read.

Setup verifies archive checksums and prepares an immutable snapshot; startup verifies
its extracted files. The service prewarms the default context and retains at most two
engines. One validation executes at a time, with eight additional admitted requests;
excess work receives HTTP 429 and queue waits are bounded to ten seconds. A stuck
validation terminates the JVM after at most 150 seconds; Compose restarts and prewarms
it. Callers receive a failure and must explicitly retry. Health checks remain available.

The container has a 2 GiB Java heap and a 4 GiB memory limit. Package indexes and
terminology caches use a private Docker volume, separated by snapshot identity.
Temporary files use a bounded tmpfs. Submitted instances and results stay in memory; there
is no instance cache or request logging. Findings may contain input values.

```toml
[validator]
timeout_seconds = 180
service_url = "http://127.0.0.1:55433"
# Optional separate online service:
# online_service_url = "http://127.0.0.1:55434"
# terminology_endpoint = "https://tx.fhir.org/r4"
```

Offline mode uses HL7's prohibited-network policy and disables remote terminology.
The container uses Docker's normal bridge network, so this is application policy,
not OS-enforced network isolation. Local terminology checks may still run; remote
expansion/membership checks remain unavailable.

Online terminology requires a separate process because HL7's network policy is global.
Configure the matching endpoint and online URL above, then start it explicitly:

```bash
SPECFHIR_TERMINOLOGY_ENDPOINT=https://tx.fhir.org/r4 \
  docker compose --profile online up -d --build --wait validator-online
```

Requests must explicitly select `--terminology-mode online`. Coverage depends on the
configured server. Online server interoperability is not exercised by the offline smoke.
See [VALIDATOR_SERVICE.md](VALIDATOR_SERVICE.md) for lifecycle decisions and checks.

### Runtime and package reproducibility

HL7 Validator **6.10.4** is pinned by SHA-256 in `validator.py`. The runtime is from
the [official release](https://github.com/hapifhir/org.hl7.fhir.core/releases/tag/6.10.4).
The [pinned upstream loader](https://github.com/hapifhir/org.hl7.fhir.core/blob/6.10.4/org.hl7.fhir.validation/src/main/java/org/hl7/fhir/validation/service/ValidationService.java)
loads terminology/extensions even for base R4 validation. Their exact archive pins
are shipped in [validator-packages.json](src/specfhir/validator-packages.json), separate
from `specfhir.lock`, so runtime setup does not rebuild the retrieval index.

The selected retrieval dependency closure is supplied unchanged alongside those
support packages. Results report the retrieval pins, support pins, retrieval
exclusions, and HL7's actual loaded-package summary. Unexpected/missing versions
fail validation execution. HL7 skips the R5 core dependency for R4 validation and
may load/convert other transitive R5 content that retrieval excludes; these are
reported, not represented as native R4 retrieval coverage. The core R4 package and
explicit US Core profile versions match retrieval; support terminology includes
HL7's required 6.2.0 alongside the graph's newer versions.

To rebuild, retain `uv.lock` and `specfhir.lock`, run
`uv sync --locked`, `specfhir sync`, and `specfhir validator-setup`, then rebuild/recreate
the validator service. Package/runtime
setup may download only their pinned artifacts. A missing or changed local archive
causes validation to fail, rather than letting HL7 select an online replacement.
The loaded package summary is also checked in online mode. Changes to the validator
runtime/support pins require source review and renewed smoke checks.

Unsupported in v0.1: non-R4 input versions, XML/Turtle input, local terminology-server
implementation, FHIRPath reimplementation, snapshot generation, and automatic
profile inference. Complex FHIR rules remain owned by HL7 tooling.

### Phase 4 checks

After the application's R4 / US Core index and validator setup are present:

```bash
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir \
SPECFHIR_VALIDATOR_SMOKE=1 uv run pytest -q tests/test_phase4.py
```

The real smoke copies only lookup metadata into an isolated schema and performs
base-R4 and US-Core valid/invalid checks plus MCP/API parity. The synthetic lifecycle
check covers missing profiles/packages, unexpected package versions, service failure,
timeout, malformed output, CLI exit codes, and snapshot integrity. Existing
Phase 1–3 acceptance checks remain in the full suite.

The live lifecycle benchmark exercises warm reuse, profile isolation, concurrency,
overload rejection, LRU eviction, and optional JVM watchdog recovery:

```bash
uv run python scripts/benchmark_validator.py --restart-check
```

The restart check intentionally interrupts the validator service. Online terminology
is not contacted by these checks.

## IG onboarding and build commands

Release discovery is read-only; explicitly choose exact versions in `specfhir.toml`:

```bash
uv run specfhir packages versions hl7.fhir.us.davinci-pas --json
uv run specfhir packages list --json
```

`packages list` reports the published inventory and whether configuration, lock, index,
and the offline validator snapshot agree. An unavailable validator does not hide the
inventory; it appears as not ready.

To synchronize the index and prepare this checkout's offline validator:

```bash
uv run specfhir sync --with-validator --json
# Add --update-lock only when intentionally updating the configured package graph.
```

Snapshot identity covers package checksums and dependency resolutions, support
packages, default context, protocol, and validator binary. Documentation, embedding
settings, and download URLs do not change that identity. The full index lock digest
remains in index metadata and validation responses, independently of the immutable
validator manifest. This identity change requires a one-time validator refresh;
subsequent retrieval-only updates reuse the same snapshot.

A healthy validator with the matching snapshot is reused. A missing or stale service
is prepared and recreated. Runtime source changes still require an explicit Compose
rebuild/recreation. The phases are sequential: if validator preparation or recreation fails, the index may
already be updated. The command fails; repeat it to finish recovery. The optional online
validator must be recreated separately. This command requires `compose.yaml` beside the
configuration and a service URL that points to that Compose validator.

Build cases use a JSON manifest with paths relative to the manifest file:

```json
{
  "cases": [
    {
      "name": "patient",
      "instance": "patient.json",
      "package": "hl7.fhir.us.core#9.0.0",
      "profile": "USCorePatient"
    }
  ]
}
```

```bash
uv run specfhir validate-cases cases.json --json
```

All cases run and retain their results. Exit 0 means completed without error findings,
4 means completed with error findings, and 1 means at least one execution/input failure.
Warnings remain visible. Case names must be unique; each case requires a package and profile.
Validation uses offline terminology and never downloads missing packages implicitly.

PAS 2.0.1 / 2.1.0 onboarding status and the approved core exception are tracked in
[PAS_VALIDATION.md](PAS_VALIDATION.md). CapabilityStatements are now included in the
structured index; the next sync rebuilds the disposable index to include them.


Published guidance can be attached to a configured root using `[[documents]]` with
`package`, an exact-release HTTPS `url`, and `title`. Sync locks the page checksum and
indexes its IG content region as `Documentation`, excluding navigation/footer content.
It never crawls links. Changed cached or downloaded content fails checksum verification
until an intentional `sync --update-lock`. Package archives and page caches stay in
`.specfhir/`; rebuilds require those cached inputs or access to their pinned URLs.

The PAS configuration records one approved core exception: Subscriptions Backport
1.1.0 declares unavailable core 4.0.0; the effective edge selects core 4.0.1. The lock
retains `dependencies` unchanged and records `dependency_resolutions` separately.
Inventory and validation results expose the exception. Other package versions are
never substituted, and the installed PAS releases retain separate validation contexts.

### CRD and reusable preparation

CRD `hl7.fhir.us.davinci-crd#2.2.1` is an explicit root alongside both PAS releases.
Its release-specific narrative pages are selected from IG metadata and pinned
from the full publication archive in the same lock as its packages. Select it with `--package`; the default remains
US Core 9.0.0. `uv run pytest tests/acceptance/test_crd.py --live-acceptance` runs the local acceptance check
using shared fixtures under `tests/fixtures/fhir/`. See the acceptance instructions below for JUnit evidence.

CRD FHIR profiles and logical-model definitions are searchable. The validator
accepts FHIR resource instances; indexing CDS Hooks logical models and prose does
not implement validation of hook envelopes, card behavior, authentication, or
end-to-end CRD workflows. Offline terminology warnings remain visible.

`specfhir sync --json` now returns current-invocation stage timings. With
`--with-validator`, it also reports validator refresh and command total time.
Skipped stages have zero elapsed time; unchanged syncs no longer repeat an old
rebuild's preparation duration. Times are operation results, not dataset metadata.

Rebuilds reuse `.specfhir/prepared/` projections by exact package/archive content,
attached document pins, and extraction format version. Archives and document pins
are still checked; cached spool hashes are checked before reuse. Damaged derived
spools are rebuilt. Local artifact IDs are remapped before one atomic database
publication. The separate element spool avoids parsing every large terminology
resource twice during publication. Existing embedding caching remains in place.
This trades local disk space for repeat extraction speed. The prepared directory
is disposable: remove it only while no sync is running to reclaim its space.

### Discover and pin full-publication documentation

The installed PAS 2.0.1, PAS 2.1.0, and CRD 2.2.1 roots now use `[[publications]]`
instead of individually configured page URLs. Preview the selection from a locked
package before changing its documentation configuration:

```sh
uv run specfhir packages pages 'hl7.fhir.us.davinci-crd#2.2.1' --json
```

The command reads the package's `ImplementationGuide.definition.page` hierarchy
and shows selected pages and exclusion reasons. It does not download a publication,
change the lock, or crawl links. The current selection includes narrative overview,
conformance, workflow, implementation, security, and artifact-overview pages.
Generated resource renderings, external links, table of contents, downloads,
credits, and release administration are excluded.

```toml
[[publications]]
package = "hl7.fhir.us.davinci-crd#2.2.1"
url = "https://hl7.org/fhir/us/davinci-crd/2.2.1/full-ig.zip"
page_prefix = "en/"
```

Use the full-publication download linked by the exact release. `page_prefix`
selects a directory within the publication (CRD uses English pages under `en/`;
PAS uses the default empty prefix). Then run:

```sh
uv run specfhir sync --update-lock --with-validator --json
```

Sync verifies the publication's embedded `package.tgz` against the locked package
checksum, pins the ZIP hash, and pins each selected page's public URL, content hash,
and archive member. `packages list --json` includes publication pins and indexed
page inventory. The original page URL remains the search citation; raw document
inspection also records the publication URL and archive member.

ZIPs remain in `.specfhir/publications/`. Only selected HTML members are read and
cached, with archive size/path/type bounds; the ZIP is never unpacked onto disk.
Missing pages or mismatched package versions fail before database publication.
An unchanged sync verifies pins, and a missing page cache can be rebuilt from the
pinned ZIP offline. Retain the ZIP to reproduce a rebuild without network access.

Explicit `[[documents]]` entries remain supported when an IG has no suitable
publication archive. A configured archive failure never silently switches to live
pages or another release. For a new IG, first install its exact JSON package, inspect
its metadata with `packages pages`, then configure its verified publication URL.
The package remains the source of FHIR definitions and validator inputs.

Run `uv run specfhir check` after sync to verify every
selected page's indexed provenance, sibling-release isolation, and config/index
agreement. Add `--with-validator` to require validator readiness. Pytest acceptance exercises
lexical/hybrid retrieval. `--json` emits readiness evidence to stdout.
This check does not change the index.

Verified archive coverage: PAS 2.0.1 has 10 selected pages (4 excluded), PAS 2.1.0
has 11 (4 excluded), and CRD 2.2.1 has 16 (5 excluded). This expands the index from
5 to 37 publication pages. The expansion reused 48 prepared packages and rebuilt
only the three documentation-owning packages; extraction took 3.56 seconds in the
observed run. All 37 page provenance checks and version-scoped retrieval checks
passed. These counts describe the IG-declared narrative selection, not every file
or page in the publication ZIP.

The expansion passed 18 tests with real-package and Java/MCP smokes enabled, plus
dedicated PAS and CRD acceptance. A subsequent locked sync with validator checking
completed in 4.696 seconds with no rebuild or validator restart.

### Reference coverage during sync and inspection

Normal `sync` now records outgoing reference findings as part of atomic index
publication. There is no separate graph command or validation workflow:

```sh
uv run specfhir sync --with-validator --json
uv run specfhir packages list --json
uv run specfhir inspect PASClaim --package 'hl7.fhir.us.davinci-pas#2.1.0' --json
```

`sync` and `packages list` include global `reference_checks` and each package's
`reference_counts`. Package counts cover its own artifacts, not a second count of
its dependency closure. Snapshot and differential occurrences are counted
separately, with exact source JSON pointers. An unchanged sync returns the findings
from the current indexed generation without recomputing them.

`inspect` adds a `references` section with complete status counts and up to 100
outgoing findings, unresolved first. Candidate details are capped at 10, with
explicit truncation indicators. Raw resource JSON remains available under
`data.resource`; unsupported resource types report `not_checked`. `resolve` keeps
its existing contract and shares candidate selection with the reference checks.

Statuses distinguish `resolved`, `ambiguous`, `excluded`, `outside_scope`,
`not_found_in_scope`, and `unsupported`. Excluded supported definitions retain only
identity metadata and exclusion reasons, allowing known exclusions to be reported
without indexing their FHIR content. No match means no evidence of the exact target
in the available local identity inventory; it does not prove global nonexistence.
Canonical references never use friendly-name aliases or choose a newer version.
They resolve in the source artifact's package context, even when that artifact was
found through another root's dependency closure.

This first pass checks StructureDefinition.baseDefinition, element type profile and
targetProfile, element binding.valueSet, local contentReference, and ValueSet.compose
include/exclude valueSet imports. An absent local target in a differential-only
profile is inconclusive. Malformed projections and fields, contained canonical
fragments, external element references, instance references, HTML hyperlinks,
terminology membership, and other resource relationships are not covered.
These are retrieval findings, not HL7 validator results: they do not by themselves
block sync or alter validation findings. Acquisition or publication failures still
abort safely, retaining the previous index and its reference findings.

`timings.reference_seconds` measures the checks and their persistence inside
`publication_seconds`; it is a substage, not an additional duration to sum.
The reference schema requires one rebuild; older published generations explicitly
report coverage as unavailable until that rebuild completes.

The first live reference pass checked 75,063 occurrences: 73,614 resolved,
299 ambiguous, 17 excluded, 216 outside scope, 543 not found locally, and 374
unsupported. Checks and persistence took 15.596 seconds within the publication
transaction; the validator snapshot was reused. These are observations from the
installed graph, not a claim of complete FHIR conformance or globally missing
resources. For example, CRD's logical-model Base target is evidenced in an excluded
R5 package, while certain PAS X12 value sets have no matching local identity.

An unchanged sync with validator verification took 4.594 seconds, reused the same
findings and validator snapshot, and reported zero extraction, embedding,
publication, and reference-check time. The 20-test suite (including real index and
validator smoke checks), Ruff, Pyright, and all 37 documentation checks passed.

Completed embedded passages are now cached inside each exact package-preparation
entry, keyed by the complete model pin and preparation format. Checksums protect
reuse; corrupt entries regenerate. Rebuilds report `embedding_preparation_cache`
hits/misses, while unchanged syncs report zero work. Extraction timing includes
spool merging; embedding timing covers preparation or verification of cached
embedded passages. No embedding values or retrieval ranking rules changed.
Measured repeat-preparation and retrieval improvements, disk/memory tradeoffs,
and the expanded benchmark are documented in [PHASE3_VALIDATION.md](PHASE3_VALIDATION.md).

All commands use one `specfhir.lock` beside the selected configuration, including
`--config custom.toml`. To measure a complete rebuild or explicitly remove obsolete
derived caches, use the existing sync command:

```sh
uv run specfhir sync --rebuild --with-validator --json
uv run specfhir sync --prune-cache --with-validator --json
uv run python scripts/benchmark_search.py
uv run python scripts/benchmark_search.py --build
```

`--rebuild` republishes the same locked inputs through the normal atomic path.
`--prune-cache` runs only after successful synchronization, while holding the sync
lock. It removes recognized obsolete package-preparation generations, obsolete
embedded-passage generations, and old vector-cache SQLite files. It keeps current
lock/model/format entries and skips symlinks and unknown names. Source packages,
publications, model downloads, and validator snapshots remain intact. Cleanup is
explicit; ordinary sync never prunes caches.

The default benchmark is read-only and reports prose retrieval plus exact-version
and unavailable-target checks. `--build` instead measures model loading, uncached
inference on 512 existing passages, a complete cached rebuild, and unchanged sync.
The inference sample bypasses the vector cache; it is not a full cold corpus build.

### DTR

DTR `hl7.fhir.us.davinci-dtr#2.2.0` is an explicit R4 root. Its published dependency
closure includes CRD 2.2.1 and PAS 2.2.1; PAS 2.0.1 and 2.1.0 remain explicit roots.
Use `--package hl7.fhir.us.davinci-dtr#2.2.0` for DTR-scoped lookup, search, and
validation. The default remains US Core 9.0.0.

The existing index covers DTR profiles (including Questionnaire, QuestionnaireResponse,
and operation parameter profiles), operations, terminology definitions, and pinned
publication documentation. Profile links use the existing source-package reference
checks. Published Questionnaire and Library **instances** live in the archive's
example directory and are not indexed as conformance definitions. Their raw JSON
can still be supplied to `validate`; DTR support does not execute CQL, render forms,
run adaptive questionnaire operations, or prove end-to-end workflow conformance.
Offline terminology limitations and upstream validation findings remain visible.

Run `uv run pytest tests/acceptance/test_dtr.py --live-acceptance` after `specfhir sync --with-validator`.
It checks profile/operation retrieval, DTR-scoped workflow search, valid/invalid
synthetic Questionnaire validation versus core R4, unmodified published examples,
CLI/API/MCP parity, and inventory coherence. Pytest can retain published-example
outcomes as JUnit properties; see the acceptance instructions below.

DTR onboarding evidence (2026-09-09): 50 supported top-level artifacts plus 14
publication pages; the complete graph has 56 packages. DTR reference checks report
551 resolved, 162 ambiguous, 1 excluded, and 31 unsupported occurrences. These
counts expose retrieval coverage, not a claim that all links or semantics resolve.
Three `Basic` instances, one top-level `Parameters`, and nested/example/metadata
files are reported as skipped by the existing inventory.

The synthetic standard Questionnaire passes with zero errors/warnings; removing
`subjectType` produces one DTR error and zero core-R4 errors. Three of four published
Questionnaire examples return zero errors; `referred-questionnaire` returns ten
"Example URLs are not allowed" errors under the unchanged validator policy.
Warnings and complete outcomes are retained in the acceptance report. This check
validates examples using their supplied profiles; it does not assign a standard
Questionnaire profile to every adaptive or unprofiled example.

The initial expansion reused 51 preparation/embedding entries and prepared five.
Sync took 189.141 seconds (48.132 embedding; 117.643 publication), followed by
156.526 seconds to refresh the validator. These are one local onboarding run,
not a performance comparison or a steady-state latency guarantee.

### Readiness and acceptance

```sh
# Installed-state readiness; no instance validation or rebuilding
uv run specfhir check --with-validator
uv run specfhir check --package 'hl7.fhir.us.davinci-dtr#2.2.0' --json

# Real package, validator, CLI, and MCP acceptance
uv run pytest tests/acceptance --live-acceptance
uv run pytest tests/acceptance/test_dtr.py --live-acceptance

# Retain unmodified published-example findings as JUnit properties
uv run pytest tests/acceptance --live-acceptance -o junit_family=legacy \
  --junitxml=.specfhir/acceptance.xml
```

`specfhir check` checks config/lock/index agreement, pinned page provenance,
sibling-version isolation, and reports reference coverage. `--with-validator`
requires a ready service matching the lock; otherwise that requirement is marked
skipped. Text output is a compact checklist; `--json` retains readiness evidence.
Exit 0 means no failed checks; exit 1 means a failed check or invalid input.
Missing publication coverage is explicitly skipped. No downloads, rebuilds,
validator restarts, instance validation, or cache changes are performed.

Pytest owns acceptance assertions. The live suite uses shared synthetic fixtures
under `tests/fixtures/fhir`, queries under `tests/fixtures/retrieval`, and one
API/CLI/MCP comparison fixture under `tests/acceptance/conftest.py`. Negative
variants are direct Python edits to fresh fixture copies. Published examples are
read unchanged from checksum-verified archives. Pytest's native assertions,
parametrization, selection, exit codes, and JUnit reporting replace custom case
manifests and assertion schemas. Published findings are retained as evidence;
service execution failures fail the test.

Live acceptance is skipped unless `--live-acceptance` is supplied. It requires
an already-synced index and ready validator and never provisions them. It targets
the repository's specfhir.toml and installed database (SPECFHIR_DSN, or its normal
local default); existing isolated tests continue to use SPECFHIR_TEST_DSN. Run
these serially with sync/build work to avoid changing the live generation mid-test.
Ordinary `uv run pytest` remains independent of installed IG/Java acceptance.

`validate-cases` keeps its existing build-input manifest and validation-error exit
code 4. It shares the bounded instance reader with `validate`; it does not become
a test framework. Benchmark scripts remain explicit opt-in measurements, using
the shared retrieval datasets where applicable. Fixture provenance is documented
in `tests/fixtures/README.md`.
