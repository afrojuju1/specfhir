# SpecFHIR — Design and work plan

Phases 1–4 and the PAS, CRD, CDEX, and DTR release expansions are implemented.
This file is the current contract; [README.md](README.md) owns operating commands,
[VALIDATION.md](VALIDATION.md) owns dated evidence, and Git retains superseded plans.
Implement only requested milestones. Do not introduce custom FHIR semantics.

## Purpose and boundaries

Provide local, version-aware published FHIR evidence through shared Python, CLI,
and MCP operations: `resolve`, `inspect`, `search`, and `validate`. Sync makes no
LLM calls. Search returns attributable passages, not generated clinical answers.

Index R4-compatible top-level `package/*.json` resources of these types:
StructureDefinition, SearchParameter, ValueSet, CodeSystem, ConceptMap,
OperationDefinition, ImplementationGuide, and CapabilityStatement. Preserve raw
JSON, package identity, FHIR release, canonical/business version, file paths,
JSON pointers, and document chunk provenance. Original archives preserve bytes;
JSONB preserves semantic content, not original formatting.

Reject incompatible configured roots. Inventory incompatible transitive packages
and skipped content explicitly; do not convert them. Example packages and nested
instance examples are outside the knowledge index. Selected publication prose
supplements package JSON; this is not complete ingestion of every published page.

DTR Questionnaire/Library instances can be submitted to validation but are not
indexed knowledge resources. No CQL execution or questionnaire rendering is
implemented. CRD logical models remain source definitions; no CDS Hooks workflow
engine is provided. CDEX adds no Task orchestration, attachment transfer, or
signature verification. PAS adds no transaction server or X12 implementation.

Patient records are transient validator inputs, never retrieval sources. Defer
FHIR server access, SMART authentication, UI, terminology hosting, FHIRPath,
automatic snapshot generation, graph orchestration, and additional FHIR releases
until explicitly requested and supported by established tooling.

## Reuse and ownership

| Concern | Owner |
| --- | --- |
| FHIR definitions and effective elements | Original packages and supplied snapshots |
| Conformance rules | Pinned HL7 ValidationService/ValidationEngine |
| Package acquisition | httpx plus bounded stdlib archive handling and exact metadata |
| Storage and lexical retrieval | PostgreSQL JSONB, SQL, FTS, pg_trgm |
| Embeddings and vectors | FastEmbed quantized ONNX model and pgvector |
| MCP framing | Official Python MCP SDK |
| Configuration and result boundaries | tomllib and Pydantic |
| Behavioral acceptance | pytest and shared reviewed fixtures |
| Installed readiness | Existing `specfhir check` operation |

Python 3.13/uv owns orchestration; Compose owns PostgreSQL and the Java 21 service.
Use direct SQL, existing helpers, and shared API serialization. No ORM, queue,
embedding service, reverse proxy, custom package format, or general semver solver
is needed for the current scope.

## Exact versions and effective definitions

Keep FHIR release, package name/version, artifact canonical/business version, and
element identity distinct. A query selects a package plus its locked dependency
closure. Own-package matches take precedence; dependency collisions return
ambiguity. Preserve multiple dependency versions without choosing the latest.
Exact selectors and collision-aware name aliases never fall back to semantic search.

Effective elements come from `snapshot.element`. Keep differential projections
separate. Missing or malformed representations are unavailable, with raw content
retained and projection issues reported; do not repair or synthesize snapshots.
Keep element ID, path, slice, choice notation, ordering, and representation.
Must-support is not interchangeable with minimum cardinality; missing is not false.

Reference coverage resolves published SD bases, type/target profiles, bindings,
local content references, and ValueSet compose imports within the source package
closure. It reports resolved, ambiguous, excluded, outside-scope, missing-in-scope,
and unsupported targets. These non-blocking retrieval findings are not a complete
HTML/instance graph or conformance validation. Inspection bounds findings and
candidates while retaining summary counts.

One reviewed dependency exception remains explicit: subscriptions-backport.r4
1.1.0 declares unavailable R4 core 4.0.0; acquisition resolves that edge to 4.0.1
and records `dependency_resolutions` without changing the manifest. Its definitions
remain retrieval-excluded under the declared-release policy; HL7 handles validation.
Do not generalize this exception into silent version substitution.

## Reproducible preparation and publication

Configuration selects exact roots, default context, publication sources, and model
settings. The lock records recursive exact dependencies, checksums, URLs, document
members, and model/runtime pins. Reject unsupported ranges and acquisition cycles.
Ordinary sync honors the lock; intentional changes require `--update-lock`.

Use bounded safe archive readers and verify manifests/checksums before reuse.
Discover prose through IG page metadata and prefer an exact full-publication ZIP
whose embedded package archive matches the package pin. Pin selected page bytes
and provenance. Explicit document sources handle publications without an archive;
archive integrity failures do not silently downgrade to another source.

Reuse prepared package and embedded-passage spools by exact input/model/format
identity. A disposable vector cache reuses exact model/input hashes. Preparation
happens outside the database publication transaction. Publish the full dataset
and its identity atomically under the existing advisory lock; failure preserves
the prior dataset. A pending lock must not relabel old database content as current.
Unchanged sync is a verified no-op. Explicit pruning removes only recognized
obsolete prepared/embedded/vector generations after successful sync.

Validator snapshots depend on effective package/support pins, default context,
protocol, JAR, and pinned-only loading policy. Retrieval roots, prose, embedding
settings, and download URL changes alone do not require cold engines. Coordinated
sync publishes the index before refreshing a changed validator snapshot; report
partial completion clearly so rerunning can finish service setup.

## Retrieval contract

Use English FTS, fuzzy name/title matching, and exact cosine scans. Hybrid search
uses rank fusion `sum(1 / (60 + rank))` over at most 100 candidates from each path,
with selected-package priority, stable tie-breakers, and source/text deduplication.
No approximate index, reranker, or query rewrite is justified by current evidence.

Token-aware passages retain headings and source locators; enforce the pinned
128–512-token setting (configured 256), including special tokens, without silent
truncation. Generated narratives and copyright are lexical-only. Queries use the
published model pin, and only sync downloads model files. Failure of a semantic
index's model is explicit; callers can request lexical mode.

Keep results bounded and provenance visible. Compact inspection exposes up to
100 elements, reference findings prioritize unresolved entries, and raw inspection
remains explicit. Search limits and issue caps are documented in README.

## Validation contract

Resolve explicit and instance-declared profiles in the selected package closure.
Without an explicit profile, request base R4 validation; do not infer a profile
from the default package. Delegate every FHIR rule to HL7 and return its issues.
Separate completed/failed execution, severity counts, and limited/unknown coverage.
Zero errors never promise full terminology or reference coverage.

The small Java HTTP adapter owns bounded engine lifetime, admission, and deadlines.
It initializes exact pinned contexts, then disables HL7 package installation from
instance profile requests while retaining reference-check policy. Service and
Python loaded-package guards detect unexpected packages; evict contaminated engines.
A stale snapshot or missing service is an execution failure, not a valid instance.

Two LRU engines and serialized execution bound mutable HL7 state. Explicit queue
and execution deadlines prevent indefinitely occupied service capacity. A stuck
JVM exits for Compose to restart; callers see failure without automatic retries.
Offline is the default HL7 network policy; optional online terminology runs in a
separate process with an explicit HTTPS endpoint. Inputs and outcomes stay in memory;
private writable caches hold package/terminology data only.

## Completed milestones and acceptance

- Foundation: locked packages, exact lookup, effective/raw inspection, atomic sync.
- Agent retrieval: FTS/fuzzy search, shared CLI/API/MCP, local pinned embeddings.
- Delegated validation: persistent Compose service, exact contexts, build manifests.
- IG expansion: two configured releases each of PAS, CRD, CDEX, and DTR; pinned
  publication prose, dependency-aware references, shared positive/negative fixtures.
- Reuse: prepared and embedded spools, measured SQL tuning, explicit cache cleanup,
  pytest acceptance consolidation, retrieval-independent validator identity.
- Current hardening: interleave CLI/MCP replay per case while retaining every API
  comparison; inspect one representative page from every sibling release with
  dependency-aware provenance; assert reviewed published-example error categories
  and counts; consolidate operating/design/evidence documentation.

Acceptance must preserve isolation, atomic-failure behavior, exact provenance,
positive/negative validator findings, and identical API/CLI/MCP results. Published
examples are original checksum-verified archive bytes; synthetic fixtures are
separate. Store reviewed error expectations once in fixtures and retain complete
published outcomes in JUnit. A changed error category/count requires review.

## Next work, when requested

Expand through the existing package/publication configuration and pytest paths.
For each release, verify exact dependencies, documentation provenance, representative
positive/negative validation, release isolation, and source-backed retrieval cases.
Use installed readiness plus live acceptance as the completion gate.

Measure before adding infrastructure. Remaining known prose misses and full database
publication cost are candidates for targeted work if they matter to actual usage.
Do not replace the embedding model, add ANN, or build incremental SQL publication
without a broader evaluation or measured latency/build requirement.
