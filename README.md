# SpecFHIR

Local, source-backed FHIR knowledge for agents. **Phases 1–3 are implemented:** package
sync, exact resolution, snapshot/differential/raw inspection, lexical/hybrid search,
and local MCP. Delegated validation remains planned in [PLAN.md](PLAN.md).

## Run

Requires uv and Docker with Compose. Python 3.13 is selected by .python-version.

```bash
docker compose up -d --wait
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
docker compose up -d --wait
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
serves three read-only stdio tools: `resolve`, `inspect`, and `search`. It handles
protocol framing and structured tool results; the tools share the Python API and
serialization/error boundary with the CLI. Logs use stderr. There is no HTTP
listener, sync tool, validation placeholder, or implicit package download.

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
docker compose up -d --wait
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
