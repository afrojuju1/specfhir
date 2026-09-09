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

The checked R4 4.0.1 / US Core 9.0.0 graph contains **18 packages**,
**25,033 artifacts and 90,048 elements**. The lexical-only extraction produces
121,738 passages; enabling embeddings splits eligible passages further to respect
the tokenizer limit. `sync --json` reports the published passage/vector counts:

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

The reviewed cases in [tests/search_queries.json](tests/search_queries.json) cover
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
[tests/semantic_queries.json](tests/semantic_queries.json), reporting expected-source
rank in the top five, median of three warm requests, preparation time, database
size, and model/cache disk usage. These are a small acceptance set, not a general
FHIR retrieval-quality benchmark. Initial indexing is CPU-bound; the tested local
setting uses eight ONNX threads and length-grouped batches of 64.

Measured results and known retrieval misses are recorded in [PHASE3_VALIDATION.md](PHASE3_VALIDATION.md).
The current semantic index contains **130,444 passages and 101,389 vectors**.


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
terminology caches use tmpfs. Submitted instances and results stay in memory; there
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
