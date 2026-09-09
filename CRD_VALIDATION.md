# CRD 2.2.1 onboarding

CRD is installed as `hl7.fhir.us.davinci-crd#2.2.1`, alongside PAS 2.0.1 and
2.1.0. US Core 9.0.0 remains the default context. This release was selected from
[HL7's published release](https://hl7.org/fhir/us/davinci-crd/2.2.1/en/index.html)
and checked against registry metadata and the downloaded package manifest.
All dependency versions and archive hashes are pinned in `specfhir.lock`.

The release adds six packages to the previously installed graph: CRD 2.2.1,
HRex 1.2.0, CDS Hooks 3.0.0-ballot, CDS Hooks Library 1.0.1, and terminology
6.2.0 and 7.0.1. The ballot dependency is declared by the published CRD package;
it was not selected as an upgrade or substituted for another release.

The initial onboarding pinned three publication pages (the later archive expansion
now indexes 16 CRD pages; see README.md, “Discover and pin full-publication documentation”):

- [Foundational Requirements](https://hl7.org/fhir/us/davinci-crd/2.2.1/en/foundation.html)
- [Supported Hooks](https://hl7.org/fhir/us/davinci-crd/2.2.1/en/hooks.html)
- [CDS Hooks Response Profiles](https://hl7.org/fhir/us/davinci-crd/2.2.1/en/cards.html)

## Reproduce

```sh
uv run specfhir sync --with-validator --json
uv run pytest tests/acceptance/test_crd.py --live-acceptance
uv run specfhir validate-cases .specfhir/crd-acceptance/cases.json --json
uv run pytest tests/acceptance/test_pas.py --live-acceptance
```

The CRD check records published examples without changing them, validates separate
synthetic DeviceRequest and ServiceRequest fixtures, and checks deliberately
missing CRD-required fields against both the CRD profile and base R4. It also
checks exact artifact version resolution, lexical/hybrid workflow retrieval,
CLI/API result parity, loaded-package context, and config/index/service agreement.
All input fixtures and full results are generated under `.specfhir/crd-acceptance/`.

## Scope

This is package knowledge and delegated FHIR validation. CDS Hooks logical models
are indexed as supplied StructureDefinitions; SpecFHIR's instance validator still
requires a FHIR resource object. Hook request/response envelopes, service discovery,
card presentation, authentication, response deadlines, and end-to-end payer/EHR
behavior are not certified by these checks. The selected narrative pages are not a
crawl of the complete publication. Terminology validation remains offline and
reports its limitations and warnings without suppressing findings.

## Verified results (2026-09-09)

The CRD acceptance script passed. The synthetic DeviceRequest and ServiceRequest
both produced **zero errors and one warning** (text-only order code; the profile
expects a code from its value set). Each contains its referenced synthetic patient
and practitioner, so unresolved external references are not hidden or ignored.
Removing DeviceRequest.status or ServiceRequest.authoredOn produces the expected
CRD minimum-cardinality error, while the same input has zero base R4 errors.
Warm median validation time over three requests was **42.00 ms** and **44.23 ms**,
respectively. These small fixtures are not a general throughput benchmark.

The untouched published examples produced four and five errors respectively in
this offline, standalone validation context. Their complete findings remain in
`results.json`; the synthetic acceptance does not assert that upstream examples
or complete workflows pass. The CRD context reports its excluded R5 dependencies
and example package explicitly in the result's retrieval exclusions.

The new index contains **51 packages, 105,231 artifacts, 120,585 elements,
291,648 passages, and 217,523 embeddings**. The first sync reused 214,184 cached
embeddings. Its observed stage timings were:

| Stage | Seconds |
|---|---:|
| Acquisition and pin verification | 6.709 |
| Extraction and initial preparation-cache population | 39.724 |
| Embedding preparation | 134.510 |
| Atomic database publication | 98.449 |
| Analyze | 2.988 |
| Total index sync, including other overhead | 282.523 |
| Validator snapshot refresh | 142.685 |
| Complete sync command with validator refresh | 425.209 |

A subsequent extraction-only run reused all 51 prepared packages in **3.254 s**,
about **12.2x faster** than initial extraction/cache population. Package inventory
and non-passage counts matched the published run. Extraction produces 275,912
passages before the embedding stage performs its additional chunking; the final
published passage count is 291,648. The prepared cache uses about **2.6 GB**.
These are single local observations with different cache states, not a controlled
end-to-end speedup claim. Full publication remains atomic and still processes the
whole dataset; incremental database mutation has deliberately not been added.

An unchanged `sync --with-validator` completed in **4.067 s**: acquisition and
integrity checks took 4.050 s, skipped extraction/embedding/publication/analyze
stages reported zero, and the matching validator was reused in 0.006 s. It returned
zero preparation-cache hits/misses because preparation was not invoked.

Ruff, Pyright, formatting, and diff checks passed. The complete test suite with
real-package and Java/MCP smokes enabled passed: **17 tests in 103.88 s**. Tests
use isolated schemas; run them after sync finishes because the synchronization
advisory lock is database-wide.

Dedicated PAS acceptance also passed after CRD onboarding: both PAS releases retain
valid request/response fixtures, expected version-specific errors, CLI/API/MCP
parity, and warm engine reuse. Its four-case build manifest still reports zero
errors. Evidence is regenerated under `.specfhir/pas-acceptance/`.
