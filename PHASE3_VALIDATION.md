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
