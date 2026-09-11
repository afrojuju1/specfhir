# Shared fixtures

`fhir/` contains reviewed synthetic resources, not patient data. Both versioned PAS
request/response fixtures are retained because the releases contain real structural
differences. CRD, CDEX and DTR fixtures were authored for their profile constraints.
The same synthetic CDEX Task and CRD orders exercise both installed releases.
Invalid variants are created by direct field deletion in the tests, not copied JSON.
Each load returns fresh JSON; the committed fixture remains unchanged.

The PAS fixtures were frozen from the successful onboarding run (commit 9dc2b61).
The previous fixture helper added referenced example resources to bundles, replaced
narratives and example identifier namespaces, and omitted optional unitPrice from
the 2.0.1 response to avoid its known offline currency issue. The shared Claim Inquiry
fixture omits identifier to expose the difference between PAS releases.

Published examples are separate acceptance inputs: tests read their original bytes
from the checksum-verified locked archive. No fixture transformations are applied.
Complete outcomes can be retained in pytest JUnit properties.

`retrieval/` contains source-backed query datasets. The search benchmark and core
retrieval tests consume the same existing datasets; IG acceptance uses ig_queries.json
with pytest parametrization for lexical and hybrid modes. Each IG case names a
reviewed expected canonical page or definition that must occur within the top five
results from the requested package. Page expectations come from the pinned publication
content; definition expectations come from the package JSON. No second expectation DSL.

`published_errors.json` records the reviewed HL7 error message IDs and occurrence
counts for each exact package/archive member under validator 6.10.4. Acceptance
asserts completed execution, no issue truncation, and exact error categories/counts;
new fatal findings also fail. Warnings remain in the full JUnit outcome without a
frozen baseline. On an intentional package or validator update, review the complete
new outcomes before editing expectations; never regenerate them merely to pass.
The lock owns archive checksums, so this file does not duplicate them or resource JSON.

Acceptance runs every reviewed case through the shared API. In each pytest worker
session, the first result for each tool, status and CLI exit class is also compared
with the real CLI and MCP transports. This retains each observable transport contract
without rerunning every domain assertion three times. Focused custom-config transport
smokes remain separate.

The release-comparison workflow reuses archived StructureDefinition JSON as well as
published examples. Reviewed PAS Claim Inquiry expectations are: identifier minimum
0→1, patient must-support absent→true, and an added authored identifier differential.
Every direct reported value is checked against its source pointer in the original
archive; large previews are checked by hash. Synthetic comparison edge cases live
in `test_comparison.py` using the existing package/profile helpers, without duplicate
FHIR fixture JSON or a second expectation format.

Package comparison checks every non-documentation source hash against those same
archives. Reviewed dependency expectations come from PAS and CRD package manifests:
PAS 2.1.0 adds a direct HREX 1.1.0 pin, retaining transitive HREX 1.0.0. The unchanged
Claim Inquiry base URL resolves to changed Claim Base JSON across PAS releases.
The existing synthetic package helper supplies unchanged-source/changed-dependency,
missing and ambiguous target cases; no additional copied resource fixtures are needed.

Multi-context validation reuses `pas-inquiry-without-identifier.json` unchanged in
both PAS releases. HL7 `Validation_VAL_Profile_Minimum` at `Claim` reports the required
identifier only in PAS 2.1.0. A minimal base R4 Patient verifies unchanged findings
across US Core 3.1.1, 6.1.0 and 7.0.0. JUnit retains the complete bounded matrix outcomes.
Synthetic replies in `test_validator.py` verify one execution per context and explicit
unknown columns when a context fails; no copied package JSON fixtures are introduced.

M4 extends the shared retrieval query list with reviewed core R4 topics. Live publication
acceptance exercises comparison → release-scoped search → full passage continuation,
including dependency-owned core guidance and missing anchors. Direct and archive page
provenance use the same installed check. Synthetic HTML in `test_guidance.py` verifies
section selection, source links, offline reuse, and dataset-guarded continuation; no
publication HTML is copied into test fixtures.

The US Core expansion adds six source-backed guidance cases across 7.0.0 and 9.0.0 to
the same query list. One shared acceptance workflow verifies version comparison,
release-scoped search and bounded passage inspection; the transport fixture retains one
API/CLI/MCP contract replay instead of duplicating every query across transports.
