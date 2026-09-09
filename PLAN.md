# SpecFHIR — Implementation Plan

Status: Phases 1–3 implemented; Phase 4 remains planned. See README.md for runnable
commands and the actual package coverage.

## 1. Purpose

SpecFHIR is a local, version-aware FHIR knowledge system for AI agents. It
indexes published FHIR packages and provides structured lookup, source-backed
search, inspection, and delegated validation through one Python API, a CLI,
and MCP.

**Structured knowledge first. Semantic retrieval second. Every result is
attributable to a specific source package and artifact.**

SpecFHIR returns evidence, not LLM-generated answers. Sync makes no LLM calls.
Local embedding inference is allowed. Retrieval works offline after a successful
sync. Downloads and optional online terminology validation are explicit network
operations.

## 2. Scope and boundaries

The completed v0.1 supports:

- FHIR R4 4.0.1, with US Core as the first integration target.
- Other compatible R4 packages using supported package formats and resource types.
- StructureDefinition, SearchParameter, ValueSet, CodeSystem, ConceptMap, and
  OperationDefinition; preserve ImplementationGuide metadata for attribution.
- Exact resolution, profile/element inspection, FTS, fuzzy matching, vector search,
  and package provenance.
- A shared Python API, CLI, local stdio MCP server, and HL7 Validator integration.

“Other packages” does not mean all FHIR releases, all package types, or every
artifact format. Report skipped resource types and unsupported package content.
Reject incompatible configured roots. Retain incompatible transitive dependencies
in the locked graph and inventory, but explicitly exclude their artifacts from R4
queries. Do not convert them silently. Packages without an explicit R4 declaration
are excluded until a compatibility policy is verified. Example packages are retained
in the graph but their instance content is not indexed.

Defer FHIR server access, SMART auth, UI, HL7 v2, C-CDA, X12, terminology hosting,
GraphRAG, agent orchestration, FHIRPath execution, full published-site ingestion,
and automatic snapshot generation.

Patient records are not a knowledge source. Validation accepts transient instance
inputs, but must not index them or retain their contents in logs or persistent
application storage. Tests use synthetic fixtures.

## 3. Reuse decisions

| Concern | Approach | Do not build |
| --- | --- | --- |
| FHIR definitions | Published packages and original FHIR JSON | A parallel FHIR object model |
| Effective profile elements | Supplied StructureDefinition snapshots | A differential merge algorithm |
| Validation | Pinned HL7 Validator CLI | Validation semantics or a custom validator |
| Package acquisition | Standard FHIR registry protocol and package metadata | A private package format or general dependency solver |
| Structured and text queries | PostgreSQL JSONB, SQL, FTS, pg_trgm | A search engine or ORM framework |
| Semantic search | FastEmbed and pgvector | An embedding service or vector database |
| MCP transport | Official MCP Python SDK | Protocol handling |
| Configuration and basic parsing | Python tomllib, json, pathlib, hashlib, tarfile | Custom parsers |

Before implementing acquisition, evaluate existing FHIR package tooling against
the actual R4/US Core dependency graph. The FHIR project's package loader is an
existing option, but it is a Node module: do not add a second runtime solely for
basic HTTP downloads. If no suitable Python tool fits, a small bounded downloader
using httpx and standard-library archive/JSON handling is acceptable. It must
support exact dependency versions, reuse downloads, detect cycles, and reject
unsupported version expressions with an actionable error. No custom semver solver.

If later requirements demand snapshot generation, terminology operations, or
FHIRPath evaluation, integrate established tooling only after verifying its
release support and behavior on representative fixtures.

## 4. Runtime and dependencies

Use Python 3.13 and uv; PostgreSQL through Docker Compose; psycopg 3 for explicit
SQL; Pydantic for SpecFHIR request/configuration/result boundaries; httpx for
downloads; Typer for CLI; and the official MCP Python SDK.

Add FastEmbed and pgvector when implementing semantic search. Start with
BAAI/bge-small-en-v1.5 as the candidate model and verify its retrieval quality on
the acceptance queries. Pin the actual model artifacts, dimensions, and relevant
embedding settings. Use pg_trgm with the text-search phase.

Use pytest for meaningful behavioral checks and Ruff plus one type checker
(initially pyright). Add respx only if HTTP tests need it. Use standard json until
measurement justifies orjson. Do not install fhirpathpy without an execution feature.

The validation phase adds a pinned validator JAR and a compatible Java runtime.
Verify dependency compatibility and runtime requirements when producing uv.lock
and the Compose definition; this plan does not assert an untested version matrix.

No SQLAlchemy, Redis, workers, FastAPI, separate vector service, or frontend.

## 5. Configuration, lockfile, and rebuilds

Proposed user configuration:

```toml
packages = [
    "hl7.fhir.r4.core#4.0.1",
    "hl7.fhir.us.core#9.0.0",
]
default_package = "hl7.fhir.us.core#9.0.0"

[embedding]
enabled = false # Enable in the semantic-search milestone.
model = "BAAI/bge-small-en-v1.5"
```

The initial package pair is a target to verify through its downloaded manifests
and artifacts, not a substitute for checking the full dependency graph.

- Keep specfhir.toml, specfhir.lock, uv.lock, and compose.yaml outside .specfhir/.
- specfhir.lock records exact resolved package identities, dependency edges,
  archive checksums, and acquisition sources. Include model/tool pins when used.
- Ordinary sync honors the lock and rejects configuration mismatches or changed
  archive bytes. An explicit `sync --update-lock` prepares a new resolution.
- Exact dependencies are supported first. If a package requires a version range,
  use a proven resolver or require an explicit exact override that is validated
  against the declared constraint. Do not guess which version is compatible.
- Runtime state records the lock digest and indexing/embedding configuration used
  by the successfully published dataset. A pending lock change must not relabel
  older database content as current.
- All .specfhir/ contents are generated: database files, packages, models, and
  validator cache. Bind the local Compose data directory there; do not silently
  place authoritative state in a named Docker volume.

The proposed rebuild procedure, once implemented, is:

```bash
docker compose down
rm -rf .specfhir
docker compose up -d --wait
uv run specfhir sync
```

Compose owns database startup. Sync checks readiness and gives an actionable
error; it does not become a process supervisor. A clean rebuild requires network
access to the locked sources. Offline retrieval requires an existing synced
database; offline resync requires all necessary cached inputs.

## 6. Version and resolution contract

Keep these identities distinct:

- FHIR release: for example 4.0.1.
- Package identity: package name plus package version.
- Artifact identity: canonical URL plus optional artifact business version,
  located within a specific package.
- Element identity: owning artifact plus ElementDefinition.id and representation.

Multiple package versions may be stored. Queries select a root package and its
locked dependency closure, defaulting to default_package. Filter this context
before ranking search results. Preserve multiple exact versions of the same
dependency in a root closure; do not unify or replace them. The selected package
owns its matching definitions. When it contains no match, search the dependency
closure and return ambiguity if multiple definitions match. Explicit package
selection can therefore disambiguate without choosing the newest version.

Resolve canonical URLs, resource IDs, and artifact names by exact matching within
that context. Allow a separate artifact-version selector and an element selector.
Artifact names ending in `Profile` also have a suffix-free convenience alias,
with the same collision checks: the actual US Core name is `USCorePatientProfile`.
The convenient `USCorePatient.identifier` syntax expands to an artifact lookup
and an element lookup; it is not FHIRPath. Canonical URLs must not be parsed by
blindly splitting on dots.

An unambiguous match returns its provenance. Multiple matches return `ambiguous`
with candidates; zero matches return `not_found`. Do not choose a lexical first
match, newest version, or semantic substitute. Package selection is the way to
disambiguate duplicates across packages.

## 7. Effective profile and element behavior

Use snapshot.element for effective inspection and retain differential.element
separately for authored changes. A differential-only profile remains searchable
and its raw content inspectable, but effective element resolution returns
`effective_definition_unavailable`. Do not silently fall back to differential
data. Malformed snapshot/differential
representations (including duplicate element IDs) are retained in the original
artifact, marked with projection issues, and excluded from element projections.
Inspection of an affected representation reports unavailable and points to raw
inspection; no automatic repair is performed.

Element path is searchable but not unique. Preserve element ID, path, slice name,
order, and snapshot/differential origin. Distinguish slices and choice elements;
a selector matching multiple slices returns candidates. Do not create an implicit
FHIRPath evaluator or recursive datatype expansion.

Compact element inspection includes cardinality, types and target profiles,
must-support, bindings and binding strength, fixed/pattern values, invariants,
slicing, definitions, and comments when present. Extract these from the element
JSON without remodeling all of them as SQL columns. Missing is not equivalent to
false. Must-support is not synonymous with minimum cardinality or mandatory data
in every instance; preserve applicable source guidance.

“Effective” means the published snapshot in its package context. It does not
claim to reconstruct the original author of each inherited field.

## 8. Storage and provenance

Start with these tables and only the projections used by real queries:

| Table | Responsibility |
| --- | --- |
| packages | Name, version, FHIR compatibility metadata, checksum, manifest |
| package_dependencies | Exact edges between package versions |
| artifacts | Package, resource type/id, canonical/version, name/title, file path, raw JSONB |
| elements | Artifact, representation, element ID/path, slice name, order, common projections, JSONB |
| documents | Artifact/optional element, kind, source locator, text/hash, FTS vector, optional embedding |
| index_state | Published lock digest, schema/index format, embedding identity, completion metadata |

Use database constraints for package identity and per-artifact element identity.
Do not enforce canonical URL uniqueness across all packages. Preserve original
package archives: JSONB preserves resource content semantically, not original
byte formatting. Store file paths and JSON pointers for precise attribution.

Every returned fact or passage carries package name/version, FHIR release where
known, artifact canonical/version or resource ID, and source locator. Human-facing
documentation links are additional provenance only when they can be established
from source metadata; do not invent URLs.

## 9. Sync behavior

1. Read configuration and validate the lock relationship.
2. Download missing locked packages and validate archive checksums and manifests.
3. Traverse dependencies; detect cycles, missing dependencies, incompatible
   releases, unsupported constraints, and identity collisions.
4. Parse supported artifacts and prepare projections/documents outside the database
   publication transaction. Apply input-size limits and safe archive handling;
   reject traversal paths, links, and malformed required inputs.
5. Generate embeddings locally when enabled. Model failure is explicit; it does
   not silently publish an allegedly complete hybrid index.
6. Publish the complete configured dataset and index_state in one PostgreSQL
   transaction, serialized with a database advisory lock. Concurrent sync attempts
   must not overwrite each other from stale configuration.
7. Report package/artifact/document counts, skipped content, and completion state.

Start with a full rebuild when source or indexing configuration changes, and a
no-op when the published identity matches. Transactional replacement is sufficient
for v0.1; do not build a scheduler, job system, staging service, or incremental DAG.
Queries see either the old committed dataset or the new one. A failed sync leaves
the old dataset available. Removed configured packages disappear from the published
dataset unless still required as dependencies; old download cache files may remain.

Define determinism as stable locked inputs, identities, extraction, and exact
lookup. Text ranking uses stable tie-breakers. Do not promise bit-identical vector
scores across different inference hardware or library versions.

## 10. Search and guidance coverage

Index artifact descriptions, resource narratives, and useful element text from
installed packages. Strip narrative markup using a safe parser; never execute it.
Attach exact source pointers. This is package-contained guidance, not a guarantee
of complete published implementation-guide prose.

Start with one document per meaningful artifact text section or element. Split
oversized passages at paragraph boundaries with explicit model-aware limits, and
retain source context. Avoid embedding raw JSON, repeating entire snapshots in
each document, or embedding every terminology code by default.

Retrieval behavior:

1. Exact structured resolution for explicit identifiers.
2. PostgreSQL FTS for prose and pg_trgm for name/title typo matching.
3. When enabled, vector candidates combined with lexical candidates using a
   small, documented reciprocal-rank-fusion implementation.

Return ranked evidence with retrieval method and source locator, not an answer
invented from the passages. Deduplicate passages, bound result counts, and keep
package context visible. Begin with exact vector distance queries; add approximate
indexes only when measured corpus size and latency justify them. No reranker or
LLM query rewriting in v0.1.

## 11. Shared API, CLI, and MCP

One implementation owns these operations:

```text
resolve(selector, package, artifact_version, element)
inspect(selector, package, view, element)
search(query, package, resource_type, limit, mode)
validate(instance, package, profile, terminology_mode)
```

These are conceptual contracts, not final Python signatures. Use typed request
and result boundaries, parameterized SQL, bounded responses, and explicit status
values. Inspection defaults to compact output with raw JSON available on request.
Do not hide incomplete definitions or truncated output.

CLI examples:

```bash
uv run specfhir sync
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --json
uv run specfhir search "patient identifier requirements" --limit 5 --json
uv run specfhir validate patient.json --profile USCorePatient --json
uv run specfhir mcp
```

CLI and MCP serialize the same results. CLI supports human-readable output and
stable JSON; errors use defined exit codes. MCP initially uses stdio, with logs
on stderr. MCP validation accepts JSON content rather than arbitrary filesystem
paths. Sync remains an explicit CLI operation; ordinary retrieval does not mutate
the package set or download missing dependencies.

## 12. Validation contract

Resolve the chosen profile using the same package context as retrieval. Require
an explicit profile when profile-level validation is intended; absent one, report
that only base R4 validation was requested. Include instance-declared profiles
and unresolved references in the reported validation context.

Run a pinned HL7 Validator CLI through subprocess with argument arrays, a timeout,
and temporary files. Use the locked package versions and verify that the validator
loads those versions. Prevent silent downloads or version substitutions in offline
mode. Capture machine-readable OperationOutcome output and retain relevant issue
severity, code, diagnostics, and location/expression in the returned result.

Default to offline terminology mode. Report terminology coverage as limited,
even when no validation errors were found. Online terminology checks require an
explicit mode and configured endpoint. Do not implement terminology expansion,
membership checking, or SNOMED/LOINC semantics inside SpecFHIR.

Keep three separate result dimensions: execution completed/failed, findings
including error/warning counts, and coverage complete/limited/unknown. A subprocess
failure or missing dependency is not a successful validation result. Include the
validator version, selected profiles/packages, and terminology mode. Remove
temporary instance files on normal completion and error paths; do not log inputs.

## 13. Delivery phases and acceptance

### Phase 1 — Package and structured lookup foundation

Create only the required project files, Compose setup, configuration/lock handling,
package ingestion, JSONB storage, snapshot projection, resolve, and inspect.

Acceptance:

- Ingest the pinned R4 and US Core package graph with a visible package inventory.
- Resolve USCorePatient.identifier with exact artifact/element/package provenance.
- Resolve an inherited snapshot element correctly.
- Keep sliced elements distinct; ambiguous names return candidates.
- Differential-only profiles cannot masquerade as effective definitions.
- Repeated sync creates no duplicates; failed replacement preserves prior data.
- A rebuild from locked inputs produces the same structured results.

### Phase 2 — First useful agent milestone (implemented)

Implemented package-contained documents, FTS/fuzzy search, compact JSON output, and the
stdio MCP tools for resolve, inspect, and search. Validation is added in phase 4,
not exposed as a success-shaped placeholder.

Acceptance:

- “patient identifier requirements” retrieves relevant US Core evidence in the
  top five results with source pointers.
- Maintain a small reviewed query set spanning bindings, slices, terminology
  descriptions, and guidance; expected sources are checked against actual packages.
- CLI and MCP return equivalent core results and explicit error states.
- Queries work with network access disabled after sync.

### Phase 3 — Semantic retrieval (implemented)

Implemented pinned local embeddings, pgvector, token-aware document splitting,
and reciprocal-rank fusion. Generated narratives and copyright remain lexical-only.
Completed inference is reused through a disposable SQLite cache keyed by model
pin and exact input; publication remains an atomic PostgreSQL operation.
Measure lexical and hybrid retrieval against the same reviewed queries. Keep
semantic search optional and label lexical-only mode explicitly.

Acceptance: semantic matches help representative paraphrases without weakening
exact resolution or crossing package context; changing the model forces reindexing;
model failure preserves the previous index. Record sync time, warm query latency,
and disk usage on the actual development machine before adding optimizations.

### Phase 4 — Delegated validation and v0.1 completion

Integrate the validator and expose validate through the shared API, CLI, and MCP.
Document Java setup, lock reproducibility, rebuilds, terminology limitations, and
unsupported content.

Acceptance: synthetic valid/invalid fixtures, explicit profile selection, missing
profile/dependency errors, subprocess failure/timeout, offline terminology limits,
and temporary-input cleanup behave as specified. Confirm that the validator and
retrieval use the same package versions.

Use the smallest meaningful automated checks for these behaviors, with small
fixtures plus a real R4/US Core integration smoke test. Do not create per-function
test scaffolding or exhaustive FHIR conformance tests already owned by HL7 tooling.

## 14. Repository shape

Keep source files flat and create them when the corresponding phase needs them:

```text
specfhir/
├── PLAN.md
├── README.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
├── specfhir.toml
├── specfhir.lock
├── compose.yaml
├── src/specfhir/
│   ├── config.py
│   ├── models.py
│   ├── packages.py
│   ├── db.py
│   ├── index.py
│   ├── search.py
│   ├── embeddings.py
│   ├── validate.py
│   ├── cli.py
│   └── mcp.py
├── tests/
└── .specfhir/  # generated and gitignored
```

Do not create empty modules for future phases. The Phase 1 inspection found 18 exact package versions, including several
extension/terminology versions, three R5 packages, one examples package, and
16 R4 artifacts with malformed element representations. These findings motivated
the explicit graph, exclusion, alias, and projection policies above.

## 15. Primary references

- [FHIR package specification](https://hl7.org/fhir/packages.html): package layout,
  metadata, dependencies, and registry conventions. The packaging reference is
  cross-release; indexed FHIR resource semantics remain R4.
- [FHIR R4 StructureDefinition](https://hl7.org/fhir/R4/structuredefinition.html):
  canonical identity, differential/snapshot representations, and element IDs.
- [FHIR R4 ElementDefinition](https://hl7.org/fhir/R4/elementdefinition.html):
  constraints, slicing, bindings, and must-support semantics.
- [US Core 9.0.0](https://hl7.org/fhir/us/core/STU9/): initial guide integration target.
- [FHIR package loader](https://github.com/FHIR/fhir-package-loader): existing
  package-acquisition tooling to evaluate before adding custom behavior.
- [HL7 Java core and Validator CLI](https://github.com/hapifhir/org.hl7.fhir.core):
  authoritative implementation and release artifacts for delegated validation.
- [HL7 Validator overview](https://info.hl7.org/hubfs/FHIR%20Open%20Source%20Tooling%20Webinar%20Series/Open%20Source%20Tooling%20-%20FHIR%20Validator%2020241015%20David%20Otasek.pdf):
  validator operation and offline terminology limitations.
