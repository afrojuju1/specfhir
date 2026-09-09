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

Acceptance calls the shared API first, then compares CLI and MCP results for each
recorded case in sequence. One MCP session serves each module. Interleaving those
transport replays reuses resident validator engines without dropping comparisons.
