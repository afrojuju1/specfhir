# Phase 3 validation

Measured on 2026-09-09 with the locked R4 / US Core 9 package graph, on an Apple M4 Max (16 CPU cores, 48 GiB RAM), macOS 26.6.2. PostgreSQL 17 / pgvector 0.8.6 runs in local Docker. Other host work was active; timings are development observations, not isolated benchmarks.

## Published index

- 25,033 artifacts; 90,048 element projections; 130,444 passages; 101,389 vectors.
- 16 upstream artifacts retain their existing projection issues; their raw content remains available.
- Model: BAAI/bge-small-en-v1.5, quantized ONNX revision `52398278842ec682c6f32300af41344b1c0b0bb2`, 384 dimensions, 256-token input budget. File checksums and runtime versions are in `specfhir.lock`.
- Copyright and publisher-marked generated narratives remain lexical-only.
- Final resumed sync: 1024.5 seconds, including 977.155 seconds of preparation; 43,419 vector cache hits. Earlier interrupted inference was reused: this is **not a cold full-build time**.
- Unchanged sync: 1.227 seconds.
- Allocated application tables and indexes: 1,094,524,928 bytes (1.02 GiB); excludes WAL and cluster overhead.
- Model/cache directory: 767,472,291 bytes (731.9 MiB), including disposable inference caches from testing; excludes package archives.

## Retrieval comparison

Each query ran once to warm the model/database, followed by three timed requests; latency is their median. Rank is the predefined expected resource and element in the first five results. A dash means that exact target was absent. No expected targets were changed after seeing results.

| Query | Lexical rank | Hybrid rank | Lexical ms | Hybrid ms |
|---|---:|---:|---:|---:|
| patient identifier requirements | 2 | 1 | 45.8 | 1290.9 |
| patient language binding | 4 | 3 | 22.7 | 1280.2 |
| race categories | 4 | 5 | 25.2 | 1273.9 |
| interpreter communicate healthcare | 3 | 2 | 23.2 | 1260.3 |
| administrative gender | 1 | 1 | 21.3 | 228.0 |
| How do I record that someone needs a translator at their medical appointment? | — | 1 | 21.2 | 1252.3 |
| Which tongue should the care team use when speaking to this person? | — | — | 20.4 | 1268.2 |
| Where can I put a person's medical record number? | — | — | 21.5 | 1247.2 |

Hybrid retained all five original expected targets in the top five and added the interpreter paraphrase (6/8 versus 5/8). The language paraphrase retrieved Patient.communication.preferred first and Patient.communication second, but missed the specifically expected Patient.communication.language. The medical-record-number paraphrase missed Patient.identifier in both modes. Race categories regressed from fourth to fifth. This small acceptance set demonstrates a useful semantic match, not general retrieval accuracy.

Exact cosine scans remain intentional. Fetching passage text after ranking reduced broad hybrid latency from 2.7–4.9 seconds to about 1.25–1.29 seconds, with identical full results for all eight queries. Lexical mode remains available for lower latency. Approximate indexes, reranking, and query rewriting are deferred until a broader evaluation and a latency target justify them.

## Checks

- Full suite with the real package smoke check: **8 passed**. Integration checks used isolated schemas in a separate temporary database, removed afterward.
- After narrowing the semantic SQL projection: **2 Phase 3 tests passed**, and all eight complete hybrid result payloads matched the pre-optimization results.
- Tests cover model pin changes, tokenizer bounds, explicit modes, package scope, failure preserving the prior published index, and lexical availability when inference fails.
- Real published-index MCP structured results matched the shared API for hybrid search and exact USCorePatient.identifier lookup.
- Ruff lint/format, Pyright, and source/wheel build passed.

Reproduce against the published semantic index:

```bash
uv run python scripts/benchmark_search.py
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir SPECFHIR_REAL_SMOKE=1 uv run pytest -q
```

At this Phase 3 checkpoint, validator integration was pending. Phase 4 is now implemented; see README.md for validation setup and checks. SpecFHIR does not generate clinical answers.

## Expanded graph performance follow-up (2026-09-09)

The current graph has 105,263 artifacts, 292,042 passages, and 217,917 vectors.
The model, tokenizer budget, embedding values, ranking policy, and exact package
scope remain unchanged. These are local development measurements with other host
work active, not isolated load-test results.

### Preparation

Compared the previous path (merge raw prepared packages, then prepare all embedded
passages) with the new path (reuse checksum-verified embedded passages per package).
The baseline already had every vector in its inference cache. Both new-path runs
produced byte-identical artifact, document, element, reference, and exclusion spools.
No live database replacement was needed for this comparison.

| Preparation path | Total seconds | Embedding stage seconds | Package embedding cache |
|---|---:|---:|---|
| Previous, all vectors cached | 112.167 | 108.164 | None |
| Populate completed-passage cache | 111.029 | 106.007 | 51 misses |
| Reuse completed-passage cache | 5.595 | 0.895 | 51 hits |

Repeated preparation improved about 20 times (95% less elapsed time). First
population did not meaningfully improve. These measurements exclude acquisition,
database publication, and validator refresh; they are not full-sync timings.
Unchanged sync already skips preparation and gains nothing from this cache.
The extra cached document spools occupy 1.93 GiB across the 51 packages for this
embedding pin. They remain disposable, ignored local data.

### Retrieval

The existing benchmark now contains 15 fixed queries: the original eight plus
seven covering PAS 2.0.1, PAS 2.1.0, and CRD 2.2.1 profiles and documentation.
Expected targets were chosen from source definitions before optimization. Each
mode warms once and reports the median of three requests per query. The table
summarizes the median of those 15 per-query medians.

| Mode | Before ms | After ms | Observation |
|---|---:|---:|---|
| Lexical | 33.9 | 31.7 | Essentially unchanged; no lexical optimization |
| Semantic | 934.4 | 676.3 | About 28% faster |
| Hybrid | 1029.8 | 748.5 | About 27% faster |

All 45 complete top-five result lists were identical, including source locations
and scores. Not every query improved: administrative-gender hybrid rose from
464 to 501 ms and CRD hooks from 126 to 137 ms. Treat small changes as noisy;
the main gains were in broader scans and sorts.

EXPLAIN ANALYZE showed a disk-spilling deduplication sort. A transaction-local
`work_mem = '64MB'` budgets more memory for that sort; this is a per-operation
budget, not a total connection memory cap. Semantic-only searches also skip the
unused lexical query. A more complex materialized/scoped SQL rewrite was tested
and discarded because it did not outperform the original. No approximate index
or ranking changes were introduced.

Lexical found 12/15 expected targets in the top five; semantic and hybrid found
13/15. Both found 14/15 within the top 50. The language paraphrase's expected
element ranks ninth; the medical-record-number paraphrase remains outside the
top 50. Quality has not improved in this milestone. This remains a small acceptance
set, and does not establish DTR/CDEX retrieval quality.

Run `uv run python scripts/benchmark_search.py` against the published index to
reproduce the expanded retrieval evaluation. Its JSON now includes package-aware
target ranks, top-50 diagnostics, and complete top-five results. Timing evidence
for this run is in `.specfhir/retrieval-before.json`, `.specfhir/retrieval-after.json`,
and `.specfhir/embedding-preparation-comparison.json` (ignored local artifacts).

## Follow-up: misses, full build, and cache cleanup (2026-09-09)

### Diagnosis of remaining misses

The indexed US Core Patient.identifier passage contains "An identifier for this
patient" and a requirement about numerical identifiers. It does not contain
"medical record number" or "MRN"; the supplied element's remaining mapping/type
fields do not supply those words either. This is a semantic vocabulary/context
gap, rather than evidence that extraction dropped an MRN sentence.

The language paraphrase retrieves Patient.communication.preferred and the parent
Patient.communication ahead of Patient.communication.language (rank 9). The latter
passage emphasizes ISO language/region codes. This is a ranking distinction among
closely related source elements, rather than an absent candidate.

A diagnostic experiment prepended the BGE retrieval instruction documented in the
[BAAI model card](https://huggingface.co/BAAI/bge-small-en-v1.5). It kept the translator
case at rank 1, worsened the language element from rank 9 to 16, and still missed
the medical-record-number target within 50 results. The instruction was not adopted.
No aliases, synthetic FHIR prose, reranker, or production query rewrite was added.
Evidence: `.specfhir/query-instruction-diagnosis.json`.

### Full rebuild and uncached inference

The existing benchmark's `--build` mode now measures a fixed 512-passage sample
selected from indexed embeddings, bypassing the vector cache on each inference
pass. It reconstructs the existing heading/text inputs and checks tokenizer bounds
and output vectors. It then performs a complete atomic rebuild using the current
lock and verified prepared caches, followed by unchanged sync.

| Measurement | Result |
|---|---:|
| Model initialization (excludes CLI startup) | 0.331 s |
| Uncached inference on 512 passages, three passes | 7.532 / 7.267 / 7.297 s |
| Median uncached throughput, batch 64, 8 CPU threads | 70.2 passages/s |
| Complete cached rebuild | 129.879 s |
| Acquisition | 4.240 s |
| Extraction and spool merging | 5.832 s |
| Embedded-passage verification/reuse, 51 hits | 1.134 s |
| Database publication, including reference checks | 114.819 s |
| Reference checks (publication substage) | 14.694 s |
| Analyze | 3.719 s |
| Unchanged sync | 4.246 s |

Publication accounts for about 88% of this full rebuild. The inference sample is
not a cold full-corpus run; do not extrapolate its throughput to all passage lengths
or treat unchanged sync as a rebuild. The previous 270-second rebuild included raw
preparation cache misses, so it is not a controlled full-build comparison. The
controlled preparation comparison above remains the evidence for the 20-times gain.
Evidence: `.specfhir/followup-build-benchmark.json`.

### Explicit cleanup

After the successful rebuild, `sync --prune-cache --with-validator` removed 54
obsolete prepared generations and one obsolete vector cache: 2,781,852,321 logical
file bytes (2.59 GiB). Current prepared/embedded generations, model files, source
archives, publication files, and the running validator snapshot remained intact.
The validator reported the same ready snapshot. Cleanup counts are per invocation,
not stored as dataset metadata. Evidence: `.specfhir/cache-cleanup.json`.

### Expanded acceptance

The default, read-only benchmark now covers 19 prose queries, including PAS request
and response bundle questions in both releases, plus eight exact lookups. Exact
checks verify release-specific identifier cardinality, required CRD fields,
wrong-version rejection, an absent canonical, and an unavailable package. All eight
passed. Hybrid and semantic each found 17/19 expected prose targets in the top five
and 18/19 within 50; lexical found 16/19. The original two prose misses remain.
These additional cases expand coverage; the higher hit count is not evidence of
a model-quality improvement. Evidence: `.specfhir/followup-retrieval-benchmark.json`.

All 21 tests passed with real index and validator smoke checks enabled. Existing
inventory, validator setup, validation, and MCP lifecycle checks now exercise a
custom configuration filename. Cleanup checks cover current/unknown-file retention,
symlink safety, failure before cleanup, and an explicit rebuild. Ruff, Pyright,
formatting, and diff checks passed. A final prune after the tests removes any
obsolete test-created vector cache; `.specfhir/cache-cleanup-final.json` records it.
