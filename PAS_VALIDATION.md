# PAS 2.0.1 and 2.1.0 side-by-side acceptance

Documentation coverage has since expanded to metadata-selected publication archives:
10 pages for PAS 2.0.1 and 11 for PAS 2.1.0. The initial onboarding evidence below
is retained; see README.md for the current acquisition and verification commands.

Status: implemented and verified on 2026-09-09.
Configuration includes both PAS roots alongside R4 4.0.1 and US Core 9.0.0.
The existing default remains US Core 9.0.0; select PAS explicitly with `--package`.

## Locked coverage

| Package | Archive SHA-256 | Indexed artifacts, including one technical-specification page |
| --- | --- | --- |
| hl7.fhir.us.davinci-pas#2.0.1 | 1ba4159f2a1ea000dfb1685601bfc902c603a1c59ce2127118ee0a7aec694218 | 98 |
| hl7.fhir.us.davinci-pas#2.1.0 | a0daef9c5acf6954c15ebd713477ce49962e56c0c2ac4362d1381aee8f7892df | 103 |

The combined graph has **45 packages, 98,051 artifacts, 116,821 elements,
264,133 passages, and 204,192 embeddings**. The PAS archives contain 71 and 75
StructureDefinitions respectively, plus two CapabilityStatements, two OperationDefinitions,
and one ImplementationGuide each. ImplementationGuide release arrays are handled correctly.

Published examples under `package/example/` remain separate from normative knowledge.
The PAS inventory reports 44 / 51 nested-or-metadata JSON members and one unsupported
Basic resource per release. Other skipped dependency content and the 16 existing
malformed element projections are reported by `packages list --json`.

**Dependency coverage limit:** Subscriptions Backport 1.1.0 also declares FHIR 4.0.0
in its package metadata. HL7 loads that exact package for validation, but the current
retrieval filter excludes its definitions because they do not declare 4.0.1. The
approved core-edge exception does not relabel those definitions. This exclusion is
reported in inventory and validation results, alongside the three R5 dependencies
and the core examples package. PAS profiles and their supplied snapshots remain indexed;
this milestone does not claim searchable coverage of the Backport guide itself.

The two technical-specification pages are explicitly configured and checksum-pinned:

- [PAS 2.0.1 technical specification](https://hl7.org/fhir/us/davinci-pas/STU2/specification.html)
- [PAS 2.1.0 technical specification](https://hl7.org/fhir/us/davinci-pas/STU2.1/specification.html)

Only their IG content regions are indexed as `Documentation`; navigation and footers
are excluded. Other published pages, diagrams, external specifications, Questionnaire/Library
content, and execution of workflow rules are not claimed as covered by this milestone.

## Approved dependency exception

Both PAS releases declare `hl7.fhir.uv.subscriptions-backport.r4#1.1.0`, which declares
`hl7.fhir.r4.core#4.0.0`. That core package returned 404 from both official registries.
The user approved selecting core 4.0.1 for this one dependency edge. The original
manifest and `dependencies` remain unchanged; `dependency_resolutions` records the
declared-to-selected mapping in the lock. Inventory and validation results expose it.

This follows the pinned HL7 engine's treatment of transitive core packages. It does
not select a different PAS, US Core, terminology, R4B, or R5 version. Unapproved
mappings fail verification. No archive is relabeled or rewritten.

Source: [HL7 Validator 6.10.4 IgLoader](https://github.com/hapifhir/org.hl7.fhir.core/blob/6.10.4/org.hl7.fhir.validation/src/main/java/org/hl7/fhir/validation/IgLoader.java).

The exact VSAC 0.18.0 archive comes from the official secondary registry. Cache reuse
preserves that source URL; a clean download still verifies its locked SHA-256.
The largest archive expands to 1.18 GB, within the revised 2 GB per-archive limit.
The approximately 3.6 GB package graph uses a private Docker cache volume instead
of consuming the JVM's memory budget. Instances and results are never cached by the service.

## Acceptance cases

`scripts/check_pas.py` reads the locked archives and generates fixtures and detailed
results in `.specfhir/pas-acceptance/`. It checks:

1. Exact lookup of the shared Claim Inquiry canonical in each context, with rejection
   of the other release's artifact version.
2. Lexical and hybrid retrieval of submission, inquiry, and cancellation guidance,
   with the selected package and publication URL retained in every result.
3. Published request/response example validation with unchanged findings retained.
4. Curated synthetic request and response Bundles with zero error findings under
   both releases, plus deliberately invalid requests missing the Claim patient.
5. The identical inquiry without an identifier, alternated across both engines:
   `Claim.identifier` is optional in 2.0.1 and required in 2.1.0. The newer context
   produces the additional minimum-cardinality finding; the older does not.
6. Exact loaded-package evidence, explicit core-resolution reporting, stable warm
   findings, retention of both PAS engines, and CLI/API/MCP result parity.

The published examples are **not assumed valid under strict offline settings**.
They contain example identifier namespaces, broken generated narrative hyperlinks,
and references to resources outside their Bundles. The 2.0.1 response also requires
currency terminology unavailable in the offline check.

The synthetic fixtures make narrowly documented changes: stable UUID identifier
namespaces, simple generated narratives, and referenced resources copied from the
same release's examples using a consistent Bundle URL base. The 2.0.1 response
omits optional `unitPrice` data. The validator's checks are not relaxed. These cases
cover a response without pricing; they do not prove offline currency validation.
Warnings and limited terminology coverage remain visible even in zero-error cases.

## Repeatable commands

```bash
uv run specfhir packages versions hl7.fhir.us.davinci-pas --json
uv run specfhir sync --with-validator --json
uv run specfhir packages list --json
uv run python scripts/check_pas.py
uv run specfhir validate-cases .specfhir/pas-acceptance/cases.json --json
```

Use `--update-lock` only for intentional package or documentation updates. A matching
healthy validator is reused; snapshot changes trigger explicit setup and recreation.
The build manifest contains exact package contexts and profiles. Invalid findings
produce exit 4; execution/input failures produce exit 1; warnings alone preserve exit 0.

## Measured locally, 2026-09-09

| Fixture | Cold first request (includes engine load) | Warm median, five requests | Errors / warnings |
| --- | --- | --- | --- |
| PAS 2.0.1 request | 10.817 s | 214.20 ms | 0 / 28 |
| PAS 2.0.1 response | engine already warm | 117.53 ms | 0 / 11 |
| PAS 2.1.0 request | 20.879 s | 243.15 ms | 0 / 27 |
| PAS 2.1.0 response | engine already warm | 232.17 ms | 0 / 30 |

Cold timing uses the unmodified published request; warm timing uses the corresponding
curated fixture. These are local acceptance measurements, not a general throughput
benchmark. Alternating the resident PAS contexts did not build new engines.
The lifecycle benchmark also passed six concurrent requests, queue rejection, bounded
LRU eviction, and forced JVM restart recovery in 63.41 seconds with the expanded cache.

Page caches are addressed by content checksum, so an intentional documentation update
preserves the earlier locked content for offline rebuilds. Registry fallback preserves
its exact source URL across cache reuse and clean downloads. Both behaviors have tests.

Final regression: **16 tests passed in 113.47 seconds**, with real-package and real-validator
smokes enabled. The separate PAS acceptance and lifecycle scripts passed. Ruff, Pyright,
formatting, Compose validation, and source/wheel builds passed. Online terminology was
not exercised. All runtime archives, caches, examples, and generated reports remain
under ignored storage; no instances or runtime databases are committed.

Final live checks confirmed an unchanged index and validator were both reused by
`sync --with-validator`. The generated four-case build manifest completed with zero
errors and 96 warnings. Both PAS contexts were resident and the service snapshot
matched the published index at handoff.
