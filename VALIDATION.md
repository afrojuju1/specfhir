# Validation and performance evidence

Consolidated from the former phase, PAS, CRD, and validator-service reports.
[README.md](README.md) owns runnable commands; [PLAN.md](PLAN.md) owns the current
contract. Historical counts and timings below describe their checkpoints, not
promises about a later checkout. Full superseded reports remain in Git history.

Measurements were recorded locally on 2026-09-09: Apple M4 Max, 16 CPU cores,
48 GiB RAM, macOS 26.6.2, Docker PostgreSQL 17/pgvector 0.8.6. Other host work was
active. Treat timings as development observations, not isolated load tests.

## Latest expansion checkpoint

Commit `818d0bf` added DTR release parity, expanded retrieval acceptance, and fixed
validator package loading after exact context initialization.

| Published state | Count |
| --- | ---: |
| Locked packages | 60 |
| Selected publication pages | 109 |
| Artifacts | 105,817 |
| Element projections | 137,860 |
| Passages | 315,809 |
| Vectors | 241,507 |
| Upstream projection issues retained raw | 16 |

The configured release set is maintained in [specfhir.toml](specfhir.toml).
Page counts at this checkpoint: PAS 10/11, CRD 15/16, CDEX 16/16, DTR 11/14
in ascending configured version order. This covers selected publication prose and
supported definitions, not every R4 artifact type or complete guide behavior.

- Full suite: **128 passed in 1,012.28 seconds**, including 105 live acceptance cases.
- Forty IG queries passed in both lexical and hybrid modes, with reviewed expected
  sources in the top five. This expands acceptance coverage, not general accuracy.
- The DTR addition reused 59 prepared packages and prepared one new input.
  Index sync took 145.469 seconds; validator refresh took 158.648 seconds separately.
- Unchanged coordinated sync took 6.534 seconds with no extraction, embedding,
  publication, or validator rebuild.
- Core-only validation of an instance declaring CDEX/HREX profiles completed with
  unknown-profile errors instead of installing those packages. A subsequent plain
  Patient passed. Both loaded-package guards remained enabled.

Local ignored evidence: `.specfhir/dtr-relevance-acceptance.xml`,
`.specfhir/dtr-relevance-sync.json`, and `.specfhir/dtr-relevance-reuse.json`.
These files are local run outputs and may not exist in a fresh checkout.

## Acceptance and documentation consolidation

The current milestone retains every recorded API/CLI/MCP comparison but replays
CLI then MCP for each case, avoiding a second tour of cold validator contexts.
Readiness checks every sibling release once, with dependency-aware provenance.
Published validation now asserts reviewed error message IDs and counts from
[the shared baseline](tests/fixtures/published_errors.json), including the pinned
validator version. Full outcomes are retained before assertions in JUnit.

Full verification: **129 passed in 903.44 seconds (15m03s)**. Ruff lint/format,
Pyright, diff checks, and local Markdown links/fences passed. Comparing JUnit case
identities confirmed all 128 prior cases remain, plus one error-baseline regression;
all 105 live cases and 20 published outcomes were retained.

| Same-flag local runs | Prior seconds | Current seconds | Change |
| --- | ---: | ---: | ---: |
| Full pytest suite | 1,012.28 | 903.44 | 10.8% less time |
| Live acceptance modules, including replay | 824.933 | 700.687 | 15.1% less time |
| DTR module, including replay | 394.018 | 265.874 | 32.5% less time |

This compares one current run with the preceding recorded run, not a controlled
repeated benchmark. Most savings came from DTR's expensive context loading;
CRD and PAS were slightly slower. No model, validator runtime, index, or acceptance
case was removed or weakened. The new checks tighten the error and release-scope
requirements. Evidence: `.specfhir/acceptance-consolidated.xml` and its `.log`.

Four obsolete Markdown reports were merged into this file. README now owns current
operations, PLAN owns the design/work contract, and fixture provenance remains
beside the fixtures. Historical commands that depended on removed scripts were
retired; exhaustive old reports remain available in Git.

## M1 — Version comparison foundation (2026-09-10)

Added shared `contexts` discovery and `compare` for published StructureDefinitions,
with six MCP tools total. Reads use the existing index; no package expansion,
reindex, embedding inference or validator restart was needed. The stored index
identity guards paginated comparisons and follow-up resolve/inspect requests.

Final focused acceptance passed seven tests in 26.73 seconds, including actual
CLI and MCP replay. The complete regression run exercised 132 tests in 609.49
seconds: 130 passed and two existing whole-result equality assertions required
updating for the new dataset identity. Those assertions now require unchanged
source evidence and a changed identity after publication, and passed in the
focused rerun. All 132 distinct tests have passing final results, including 106
live cases. Ruff, formatting, Pyright, diff/documentation checks and installed
readiness with the validator passed. The PAS Claim Inquiry snapshot comparison produced 61 change entries;
all pages were retrieved in groups of ten. Every direct before/after value was
verified against its pointer in the checksum-verified original package JSON;
preview-truncated values were verified by hash. Reviewed examples include identifier
minimum 0→1 and patient must-support absent→true, plus the added authored identifier
in the differential. Reverse and unchanged comparisons also passed.

The focused run's first ten-change page took 37.3 ms through the Python API and
serialized to 17,057 bytes of result data. This is one local observation, excludes
CLI startup and MCP framing, and is not a general latency benchmark.
Evidence: `.specfhir/m1-acceptance.xml`, `.specfhir/m1-final-focused.xml`, their
logs, and `.specfhir/m1-readiness.json`.

Synthetic checks cover ambiguous/missing artifacts, unavailable/malformed snapshots,
explicit renamed-artifact pairing, ownership changes, array ordering, inserted
versus reordered elements, absent/null/false values, unknown fields, large previews,
JSON-pointer escaping, pagination and stale identity rejection. A pending lock and
changed configuration default do not relabel published dataset identity. Existing
pytest fixtures and transport paths are reused; no comparison-specific database or
second acceptance framework was added.

## M2 — Package comparison and direct targets (2026-09-10)

The existing `compare` API, CLI and MCP tool now support package inventories and
reference targets. No schema, package pins, embeddings or validator configuration
changed. Each comparison reads one consistent published dataset, including exact
closure edges and sync's stored reference findings.

Focused acceptance passed five tests in 15.16 seconds, including real API/CLI/MCP
replay. PAS 2.0.1→2.1.0 has 124 owned identities (96 changed, 17 added, 11 removed),
78 dependency edges and 21 dependency-name groups: 223 pageable items in total.
All non-documentation artifact hashes were checked against checksum-verified
archives. Claim Inquiry yields 120 reference groups; its unchanged base URL points
to changed published target JSON, verified independently from both archives.
The manifest review also checks PAS's new direct HREX 1.1.0 edge while preserving
HREX 1.0.0 through CRD 2.0.0. The initial test incorrectly expected HREX to be newly
introduced to the closure; source review corrected that expectation to a changed
version set, without changing the implementation.

Synthetic cases cover a byte-identical source with a changed dependency target,
removed owned artifacts still present in dependencies, duplicate canonical identities,
unavailable profile projections, missing/excluded/outside-scope/ambiguous targets,
local self references, ValueSet imports, cycles, pagination, reverse comparison and
stale dataset rejection. Direct traversal stops at depth one; longer reference
cycles and behavioral impacts are explicitly outside the result's claims.

The focused run observed 140.5 ms / 115,074 serialized bytes for the first 100-item
package page, and 194.8 ms / 302,551 bytes for the first 100-item reference page.
These are single local Python API observations, including the result envelope but
excluding process startup and MCP framing. Smaller page limits reduce response size;
there is no controlled before/after performance claim for these new operations.
Full regression ran 134 tests in 617.84 seconds: 133 passed, and a new malformed
projection assertion hit the ownership guard first because its right-hand artifact
was dependency-owned. The test now separately verifies the ownership rejection and
unavailable projection in its actual owning package. No production code changed for
that correction. Final focused acceptance passed all five tests in 15.62 seconds,
including API/CLI/MCP replay: all 134 distinct tests have passing final outcomes.
Ruff, formatting, Pyright and diff checks passed.
Evidence: `.specfhir/m2-focused.xml`, `.specfhir/m2-acceptance.xml`,
`.specfhir/m2-final-focused.xml` and their logs.

## M3 — Multi-context validation (2026-09-10)

Validation uses an ordered `contexts` list and returns `results[]` plus an issue
matrix. The two-context prototype was replaced before commit. Each selection owns
its package and optional profile; no left/right fields or all-pairs service calls
are needed. Single-context validation and matrix requests share one implementation.

The live matrix workflow passed in 98.43 seconds through actual API/CLI/MCP replay.
PAS 2.0.1/2.1.0 retains the reviewed identifier-minimum difference. A single base R4
Patient was then validated across three actual US Core versions (3.1.1, 6.1.0, 7.0.0),
with identical issue arrays and zero errors. A failed middle context preserved its
unknown coverage and null issue column while the two successful contexts remained
comparable. JUnit retains both complete bounded matrix outcomes.

Synthetic tests exercise one, two and three contexts, assert exactly N service calls,
verify independent profile versions and unchanged input semantics, and ensure every
original issue index is represented. Failure, busy, wrong loaded packages, output
overflow, unavailable profiles, stale dataset IDs and changed snapshots stay explicit.
Duplicate selections, invalid/empty lists, extra fields, mixed input forms and context
bounds are rejected. Both repeated CLI packages and structured context JSON are tested.

Issue matching remains conservative: IDs, codes, locations and unchanged messages
are required for shared findings. Duplicate keys, changed messages or insufficient
identity are uncertain. No fixed/new-defect, coverage or conformance inference is made.
The request bound is 16 contexts and existing per-context response limits are retained;
there is no persistent input/result store or new validator service.

The complete revised regression passed **137 tests in 709.31 seconds**, including
real embedding and validator smoke tests and all existing IG transport workflows.
Ruff, formatting, Pyright and diff checks passed. No schema, package or service
configuration changes were required.

Evidence: `.specfhir/m3-matrix-focused.xml`, `.specfhir/m3-matrix-acceptance.xml` and
their logs. Earlier prototype measurements remain only in ignored local reports;
the context-list contract supersedes them.

## M4 — Connected guidance (2026-09-10)

Six checksum-pinned permanent R4 pages cover profiling/slicing, extensibility,
references, terminology bindings, bundles and conformance. The core whole-spec
ZIP was inspected: 35,698 entries, 1,795,754,302 expanded bytes, backslash member
paths and no embedded package archive. It does not meet the verified IG archive
contract. Existing direct-page acquisition is used without weakening archive guards.
Bundle selection retains 14 authored anchors and excludes generated definition tables.

Publication extraction preserves sections, headings, anchors and outgoing source
links for all 115 pinned pages. Search labels relevance candidates and carries a
dataset guard. The existing inspect operation reads complete bounded passages by
pointer or heading anchor, with stable continuation and explicit missing targets.
Readiness checks now use the same provenance/isolation checks for direct and archive
pages. No new service, dependency, FHIR interpretation or persisted query state.

Focused acceptance passed **98 tests in 88.91 seconds**, covering all existing IG
retrieval queries, seven reviewed R4 topics in lexical/hybrid modes, a PAS comparison
through release-specific guidance and bounded passage reading, API/CLI/MCP parity,
and synthetic source selection, offline reuse and stale-continuation failures.
Additional full-regression checks follow an explicit published link into another
indexed page and verify excluded generated Bundle sections remain unavailable.

The real sync completed in **263.322 seconds**: acquisition 7.820, extraction 8.175,
embedding preparation 116.736, database publication 126.384 (including 16.176 for
reference computation), and analyze 3.933. It reused 51 of 60 package preparations
and 235,057 of 242,301 embeddings. Publication-only preparation identities preserve
reuse for structured-only packages; the published index format is now 6.
A locked repeat with HTTP disabled returned unchanged in **5.810 seconds**, with
zero extraction, embedding, publication or reference work. Package/validator pins
are unchanged; no validator restart was needed. Full database publication remains
a measured existing cost, not an incremental-publication implementation in M4.

The complete regression passed **154 tests in 736.36 seconds**. Final review then
added exact-canonical-before-fragment lookup, nested heading IDs, and shared runtime
identity checks. Publication identity now includes resource, page and embedding
transformation versions; ordinary sync detects them and readiness exposes
`index_matches_runtime`. A synthetic regression verifies stale readiness before
sync and rejected continuation after it.

The final ordinary locked refresh took **197.218 seconds** and reused all **242,301
embeddings**; embedding preparation took 26.356 seconds, database publication
154.118 seconds (including 18.247 for references). These runs differ in newly
embedded content and are not a controlled throughput benchmark. The final locked
repeat with HTTP disabled returned unchanged in **4.579 seconds**.

Final verification on the completed code passed **126 tests in 206.90 seconds**:
all isolated tests (including real embedding/validator smoke tests) and the entire
publication acceptance set with API/CLI/MCP replay. This covers the final identity,
exact canonical, nested heading, malformed link and direct-source readiness changes.
All **11 installed checks** passed, including `index_matches_runtime` and validator
readiness. Ruff, formatting, Pyright and diff checks passed.

Evidence: `.specfhir/m4-final-sync.json`, `.specfhir/m4-final-offline-sync.json`,
`.specfhir/m4-final-check.json`, `.specfhir/m4-acceptance.xml`,
`.specfhir/m4-final-acceptance.xml`, `.specfhir/m4-sync.json`, `.specfhir/m4-offline-sync.json`,
`.specfhir/m4-focused.xml`, `.specfhir/m4-check.json`, and their logs.

## M5 — Broader relationships (2026-09-11)

Incoming canonical references use `inspect --view incoming` through the shared Python,
CLI and MCP operation. The target is resolved once in the selected package closure;
stored resolved-target provenance then selects direct source occurrences from that same
closure. Results retain target business version and source package/file/pointer, use
stable own-package-first ordering, and continue with the published dataset identity at
the existing 1–100 page bound. Empty targets, ambiguous selectors, artifacts without a
canonical, stale continuations and unavailable reference generations remain explicit.
Local element content references are not presented as resource-level incoming edges.

Archive review selected only relationships needed by existing workflows. PAS 2.0.1 and
2.1.0 identify two and three profiles based on their exact Claim Base definitions, and
13 and 17 bindings to `X12278RequestedServiceType`. PAS CapabilityStatements identify
the two Claim operations; their OperationDefinitions identify request/response profiles.
DTR questionnaire-package operations identify input/output profiles and, in 2.2.0, a
parameter target profile. CDEX submit-attachment identifies its input profile and nested
parameter binding; the external LOINC ValueSet remains `not_found_in_scope` rather than
being fetched or treated as resolved.

The supported extraction subset is now StructureDefinition bases/profiles/bindings/local
content references, ValueSet compose imports, CapabilityStatement REST resource
profiles/supported profiles/operations, and OperationDefinition bases, input/output
profiles, recursive parameter target profiles and bindings. Capability imports,
instantiates, guides, messages and search parameters, other OperationDefinition fields,
and Questionnaire/Library metadata remain explicitly unchecked. The latter instances
are still validation inputs, not indexed knowledge or executable content.

The extraction-version refresh published 105,823 artifacts and 82,628 reference
occurrences, reusing all 242,301 embeddings; package and validator pins were unchanged.
An ordinary locked repeat was unchanged in 4.501 seconds with no extraction, embedding,
publication or reference work. Focused source/API/CLI/MCP acceptance passed six tests in
59.50 seconds. The complete live regression passed **157 tests** with two expected skips
and no failures/errors in **650.02 seconds**. Ruff, Pyright, diff checks and all **11**
installed readiness checks passed. Evidence: `.specfhir/m5-focused.xml` and
`.specfhir/m5-final.xml`.

## Published examples versus synthetic fixtures

Twenty original published examples across eight IG releases are loaded directly
from checksum-verified archives. They are not rewritten to make validation pass.
Their reviewed baselines include prohibited example URLs, missing external
references, bundle/profile mismatches, and offline terminology findings. Some DTR
examples have zero errors. The fixture baseline is the sole maintained list of
per-example categories/counts; warnings remain visible in the complete outcomes.

Synthetic fixtures independently test intended constraints with positive and
negative cases. PAS release-specific bundles retain actual structural differences;
its 2.0.1 synthetic response omits optional pricing, so it does not prove offline
currency validation. CRD DeviceRequest/ServiceRequest original examples produce
four/five unresolved-reference errors. See [fixture provenance](tests/fixtures/README.md).

Offline coverage is limited even with zero errors. These runs do not establish
online terminology interoperability, runtime CDS Hooks/CQL behavior, or complete
conformance of every resource in the installed packages.

## Retrieval and embedding measurements

The original R4/US Core checkpoint had 25,033 artifacts, 130,444 passages, and
101,389 vectors. Hybrid found 6/8 reviewed targets in the top five versus lexical
5/8. Narrowing the semantic SQL projection reduced broad hybrid queries from
2.7–4.9 seconds to about 1.25–1.29 seconds, preserving all eight complete results.
The initial resumed sync took 1,024.5 seconds and reused interrupted inference;
it was not a cold full build.

A controlled preparation comparison on the later 51-package graph reused the
same inputs and produced byte-identical artifact/document/element/reference spools:

| Preparation path | Total seconds | Embedding stage seconds |
| --- | ---: | ---: |
| Previous path, all vectors already cached | 112.167 | 108.164 |
| Populate completed-passage cache | 111.029 | 106.007 |
| Reuse completed-passage cache | 5.595 | 0.895 |

Repeated preparation improved about 20×; first population did not improve.
This excludes acquisition, database publication, and validator refresh. The extra
51-package completed-passage cache occupied 1.93 GiB. Unchanged sync already skips
preparation and gains nothing from this cache.

Across 15 queries, median warm latency changed as follows. Each query warmed once,
then used the median of three requests; the table is the median across queries.

| Mode | Before ms | After ms |
| --- | ---: | ---: |
| Lexical | 33.9 | 31.7 |
| Semantic | 934.4 | 676.3 |
| Hybrid | 1,029.8 | 748.5 |

All 45 complete top-five lists stayed identical. Semantic/hybrid improved about
28%/27%; lexical variation was noise. Transaction-local 64 MiB `work_mem` avoided
a spilling deduplication sort, and semantic-only mode skipped unused lexical work.
This is a per-operation memory budget, not a total connection cap. No ranking,
model, or approximate-index change was introduced.

The subsequent 19-query benchmark found 16/19 lexical and 17/19 semantic/hybrid
targets in the top five, and 18/19 semantic/hybrid within 50. Eight exact lookups
passed. Two known misses remain: the language paraphrase's intended element ranks
ninth; the medical-record-number target is outside 50. The indexed identifier
passage does not mention MRN, while language competes with closely related elements.
A tested BGE instruction prefix worsened language to rank 16 and did not fix MRN;
it was discarded. Added cases are coverage growth, not model improvement.

## Build cost and cleanup

| Measured operation on the 51-package checkpoint | Result |
| --- | ---: |
| Model initialization | 0.331 s |
| Uncached 512-passage inference, three passes | 7.532 / 7.267 / 7.297 s |
| Median sample throughput, batch 64 / eight threads | 70.2 passages/s |
| Complete cached rebuild | 129.879 s |
| Database publication within that rebuild | 114.819 s |
| Unchanged sync | 4.246 s |

Publication consumed about 88% of the cached rebuild. The sample is not a cold
full-corpus benchmark; do not extrapolate across arbitrary passage lengths.
Explicit cleanup removed 54 obsolete prepared generations and one vector cache,
2.59 GiB of logical file bytes, preserving current/source/model/validator inputs.

Expansion observations, each against a different input graph:

| Milestone | Reuse | Index sync | Validator refresh |
| --- | --- | ---: | ---: |
| Initial CRD 2.2.1, 51 packages | Extraction 39.724 → 3.254 s | 282.523 s | 142.685 s |
| Initial DTR 2.2.0, 56 packages / 51 pages | 51 hits / 5 misses | 189.141 s | 156.526 s |
| CDEX + CRD sibling releases, 60 packages / 98 pages | 54 hits / 6 misses | 250.518 s | 145.016 s |
| DTR sibling release, 60 packages / 109 pages | 59 hits / 1 miss | 145.469 s | 158.648 s |

These are not controlled before/after full-build comparisons. CRD's 12.2× extraction
reuse improvement does not describe the full sync. CDEX/CRD acceptance at commit
`2d1b0a4` passed 97 tests in 505.97 seconds; the later suite includes more work.

## Validator lifecycle observations

Initial direct-port US Core validation, ten warm synthetic requests: median
13.17 ms, maximum 16.47 ms. Six concurrent requests preserved outcomes; excess
admission returned 429 in 43.54 ms. Three contexts exercised the two-engine LRU.
A forced deadline terminated the JVM and Compose restored readiness in 27.20 seconds.
These tests exercise lifecycle behavior, not general FHIR throughput.

PAS warm synthetic medians over five requests:

| Release | Request bundle | Response bundle |
| --- | ---: | ---: |
| 2.0.1 | 214.20 ms | 117.53 ms |
| 2.1.0 | 243.15 ms | 232.17 ms |

First published requests including engine load took 10.817 and 20.879 seconds.
Those inputs differ from the warm synthetic inputs, so they do not establish a
cold/warm ratio. Expanded PAS lifecycle checks completed in 63.41 seconds.
The graph expanded to about 3.6 GB before support packages, motivating the private
package-cache volume instead of spending the JVM/tmpfs memory budget.
CRD synthetic warm medians were 42.00 ms for DeviceRequest and 44.23 ms for
ServiceRequest. Engine residency and context changes materially affect timings.

The existing benchmark scripts reproduce targeted measurements; normal acceptance
does not rerun forced restarts or historical cold/warm experiments. Current runtime
limits and recovery commands are maintained only in README and Compose.
