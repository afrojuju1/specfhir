# SpecFHIR

Local, version-aware FHIR evidence for agents: exact lookup, profile inspection,
source-backed search, and delegated HL7 validation through a shared Python API,
CLI, and six MCP tools. Retrieval works offline after sync. SpecFHIR returns
published evidence, not generated clinical answers.

[PLAN.md](PLAN.md) defines the current design and scope.
[VALIDATION.md](VALIDATION.md) records dated acceptance and performance evidence.
[Fixture documentation](tests/fixtures/README.md) explains the shared test inputs.

## Run

Requires Python 3.13, uv, and Docker Compose. Java 21 is in the validator image;
no host Java installation is needed.

```bash
docker compose up -d --wait postgres
uv sync --locked
uv run specfhir sync --with-validator --json
uv run specfhir check --with-validator --json
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --view differential --json
uv run specfhir search "patient identifier requirements" --mode hybrid --json
uv run specfhir validate patient.json --profile USCorePatient --json
```

The first sync downloads pinned packages and model files, indexes the dataset,
then prepares and starts the validator. Subsequent unchanged runs reuse both.
A measured clean setup took about 23 minutes and 6.7 GiB, mostly for local embedding
generation; the unchanged follow-up took about 6 seconds. See
[VALIDATION.md](VALIDATION.md) for the dated breakdown.
`patient.json` is your input file; submitted instances are never indexed.

[specfhir.toml](specfhir.toml) selects exact package roots and publications;
[specfhir.lock](specfhir.lock) pins their dependency graph, checksums, sources,
and embedding runtime. [uv.lock](uv.lock) pins Python dependencies.
Current configured roots are R4 4.0.1, US Core 9.0.0 (default), PAS 2.0.1/2.1.0,
CRD 2.1.0/2.2.1, CDEX 2.0.0/2.1.0, and DTR 2.1.0/2.2.0.
Additional dependency versions remain distinct in the lock.

Compose binds PostgreSQL to `127.0.0.1:55432` and the validator to
`127.0.0.1:55433`. PostgreSQL uses user/database `specfhir` and local development
trust authentication. Its files live under ignored `.specfhir/postgres`.
For a different database port, set both Compose's `SPECFHIR_PORT` and the client's
`SPECFHIR_DSN`. Use a dedicated database per dataset.

Commands accept `--config /absolute/path/custom.toml`; the lock and `.specfhir`
cache follow that file's parent directory. `SPECFHIR_DSN` overrides the database
connection independently of the configuration filename.

## Lookup and search

```bash
uv run specfhir resolve USCorePatient.identifier --json
uv run specfhir inspect USCorePatient --view raw --json
uv run specfhir resolve PASClaim --package 'hl7.fhir.us.davinci-pas#2.1.0' --json
uv run specfhir search "patient identifier requirements" --mode lexical --limit 5 --json
```

Queries use the selected package and its exact locked dependency closure.
Matches in the selected package take precedence. Dependencies may contain multiple
versions; unresolved collisions return candidates rather than choosing the newest.
Use `--package` for an explicit context and `--artifact-version` for a business
version where supported; see each command's `--help`.

`resolve` matches canonical URLs, IDs, and names exactly. Names ending in `Profile`
also have a collision-aware suffix-free alias. `USCorePatient.identifier` is an
element selector, not FHIRPath. Slices and choice elements retain their literal IDs.
Effective inspection uses the published snapshot. Differential-only or malformed
projections report unavailable; `--view differential` and `--view raw` provide
explicit alternatives. No snapshot synthesis or automatic repair occurs.

Search modes:

- `auto`: hybrid if the published dataset has embeddings, otherwise lexical.
- `lexical`: PostgreSQL FTS and fuzzy name/title matching, without inference.
- `semantic`: exact cosine distance within the selected context.
- `hybrid`: reciprocal rank fusion of up to 100 lexical and 100 semantic candidates.

The configured model is quantized BGE small, 384 dimensions, with a 256-token
budget. Only sync downloads model files. Queries use verified files from the
published model pin; an unavailable model is an explicit error, with lexical mode
still usable. Generated narratives and copyright remain lexical-only.
Search accepts up to 500 characters and returns 1–50 results with bounded excerpts,
source locations, and explicit truncation. Use exact lookup for identifiers.

## Read surrounding guidance

Search results are `relevance_candidate` evidence, not proven explanations of a
computed change. Search now returns `dataset_id` and accepts it on follow-up calls.
Use the result's exact canonical, owning package and pointer to read full passages:

```bash
uv run specfhir inspect 'https://hl7.org/fhir/R4/profiling.html#cardinality' \
  --package 'hl7.fhir.us.davinci-pas#2.1.0' --view passages --limit 5 --json
```

`inspect --view passages` works for indexed resource text and publication prose.
It accepts `--pointer` to select the section/field cited by search, and `--offset`
and `--limit` (1–100, default 5). Continue using `next_offset` and the returned
`--dataset-id`; stale continuation is rejected. Without a pointer or URL fragment,
it traverses the document in section order. Each passage retains its original
pointer and chunk; chunks are bounded by extraction and the published token budget.
A section can span multiple passages. A fragment selects that exact published
heading anchor; missing or duplicate anchors remain not found or ambiguous.

Publication passages include headings, citation URLs, page checksums and up to 20
outgoing `published_link` URLs per section. These are literal publisher links,
resolved relative to the pinned page URL; they are not automatically rewritten to
another release, fetched, or claimed to explain a change. `links_total` and
`links_truncated` expose the bound; `links_unusable` counts malformed/non-HTTP links; raw inspection retains every extracted link.
Follow a link through the same scoped inspect operation. An unindexed page or
anchor stays unavailable. Search ranking and explicit hyperlinks are separate
forms of evidence; neither establishes compatibility or conformance.

## Find incoming references

Use the same exact inspection path to find definitions in a selected package closure
that directly reference one resolved artifact:

```bash
uv run specfhir inspect \
  'http://hl7.org/fhir/us/davinci-pas/ValueSet/X12278RequestedServiceType' \
  --package 'hl7.fhir.us.davinci-pas#2.1.0' --view incoming --limit 100 --json
```

Incoming results retain the target's package and business version plus each source
package, file and JSON pointer. Results are ordered and bounded; continue with
`next_offset` and `--dataset-id`. Coverage is limited to resolved canonical
relationships already recorded by sync. Local element `contentReference`, unresolved
targets, recursive traversal, instance references and HTML links are not reverse edges.
An artifact without a canonical reports `not_checked`; an exact target with no incoming
edges returns a completed empty page.

Sync records StructureDefinition bases, type/target profiles, bindings and local content
references; ValueSet compose imports; CapabilityStatement REST resource profiles,
supported profiles and operation definitions; and OperationDefinition bases,
input/output profiles, parameter target profiles and parameter bindings. Nested
OperationDefinition parameter parts are included. Inspection reports unresolved,
excluded, outside-scope, ambiguous and unsupported canonicals without fetching or
repairing them. CapabilityStatement imports/instantiates/guides/messages/search
parameters, other OperationDefinition fields, and Questionnaire/Library metadata are
outside this reviewed subset.

Core R4 guidance is pinned through six explicit permanent publication pages:
profiling (including slicing), extensibility, references, terminology bindings,
bundles and conformance. Bundle ingestion selects authored section anchors and
omits generated resource/constraint/search tables already represented by package
JSON. An optional `anchors` list on an explicit document source selects exact
sections and rejects missing or duplicated anchors. Empty/omitted selects the
whole content region. This is selected coverage, not the full R4 publication.

The R4 whole-spec ZIP has no embedded package archive and uses backslash member
paths, so it cannot meet our existing verified IG archive contract. Its selected
permanent pages use the existing checksum-pinned direct acquisition path instead.
`check` verifies both direct and archive page provenance and sibling-release
isolation. `index_matches_runtime` also checks extraction/embedding format versions
through the same dataset identity used by sync; a matching lock alone is insufficient. Ordinary locked sync reuses the downloaded pages offline.

## Compare profile releases

```bash
uv run specfhir contexts --limit 100 --json
uv run specfhir compare PASClaimInquiry \
  --left-package 'hl7.fhir.us.davinci-pas#2.0.1' \
  --right-package 'hl7.fhir.us.davinci-pas#2.1.0' --limit 10 --json
```

`contexts` exposes the actual published dataset identity, installed releases,
exclusions, coverage counts, and configuration/lock coherence without contacting
the registry or validator. Its `default_package` is explicitly labeled as the
current configuration default; it is not a historical property of the index.
Use `--package` for one exact installed package. Excluded packages remain visible
with their reason; an uninstalled package returns `not_found`.

`compare` requires two exact package contexts. It resolves the left selector and
pairs the right artifact by canonical identity, preserving each dependency closure.
Use `--left-artifact-version` / `--right-artifact-version` to select artifact business
versions separately from package versions. `--right-selector` explicitly pairs
renamed artifacts; automatic pairing never guesses a rename. A pairing that switches
between package-owned and dependency content fails and asks for the owning packages.
The result identifies actual sources and whether each came from a dependency.

The default `--mode profile` supports StructureDefinition. It compares snapshot to snapshot
(default), or differential to differential with `--view differential`. Missing or
malformed representations return unavailable. Elements match by published IDs;
renamed IDs appear as additions/removals. All fields in the selected representation
and top-level metadata are compared, including fields outside compact inspection.
Array ordering is preserved; shared-element reordering is reported separately from
insertions. A flag indicates changes in the unselected representation, whose content
is not included in the detailed comparison. These are published source differences,
not compatibility conclusions or attribution of inherited fields.

Results include counts, before/after presence and values, exact source pointers,
`dataset_id`, and `next_offset`. Limits are 1–100 entries. For the next page, repeat
the same selection/filter arguments with `--offset <next_offset>` and
`--dataset-id <dataset_id>`. A changed dataset rejects continuation; restart at
zero. Unchanged rebuilds retain the same content identity. This also applies to
`contexts` pagination. Each comparison reads both sides in one consistent transaction.

Use `--element Claim.identifier` to retrieve just that published element's changes.
Values above 2,000 JSON characters have an explicit preview, hash and length instead
of an unbounded payload. Retrieve complete values from the cited artifact with
`inspect --view raw`; raw inspection returns the full artifact. An absent value
is marked `present: false`; its pointer identifies the missing field or enclosing
array, not an existing value. The element-order item identifies its values as IDs
derived from the cited arrays.

Discovery, comparison, `resolve`, `inspect` and `validate` return a top-level `dataset_id`.
`resolve`, `inspect` and `validate` accept `--dataset-id` to reject
stale follow-up reads. For example, inspect `Claim.identifier` in the selected PAS
profile using the identity returned by comparison. No extra database, reindex, or
validator restart is required to use these operations on an existing published index.

Use the same `compare` operation for package and direct-target comparisons:

```bash
uv run specfhir compare --mode package \
  --left-package 'hl7.fhir.us.davinci-pas#2.0.1' \
  --right-package 'hl7.fhir.us.davinci-pas#2.1.0' --json
uv run specfhir compare PASClaimInquiry --mode references \
  --left-package 'hl7.fhir.us.davinci-pas#2.0.1' \
  --right-package 'hl7.fhir.us.davinci-pas#2.1.0' --json
```

Package mode omits the artifact selector. Its `items` page separates `artifact`,
`dependency_pin`, and `dependency_edge` entries, with complete counts by category.
Owned artifacts match by resource type plus canonical, falling back to exact resource
ID or file when no canonical exists. Duplicate identities and inventoried exclusions
are `uncomparable`; dependency content cannot replace a removed owned artifact.
`changed` means the full source JSON differs. `profile_details_available` indicates
whether profile mode can supply field differences in the selected view; use raw
inspection for other resource types. Counts omit zero categories/statuses.

Dependency pins include every exact version in each closure, with additions, removals,
and changes grouped by package name. Edges retain the declaring package; only the
selected root is normalized when matching edges. Both sides report dependency cycles.
For example, PAS 2.1.0 adds HREX 1.1.0 directly while retaining HREX 1.0.0 through CRD.
This is not a replacement of every HREX dependency.

Reference mode supports StructureDefinition and ValueSet using sync's existing
reference findings. It compares bases, type/target profiles, bindings, local content
references, and ValueSet imports. Identical literals group by element ID and relationship
(and ValueSet include/exclude), preserving counts and cited occurrences. Changed literals
appear as additions/removals; array positions are not guessed across releases. Target
identity changes and target content changes are distinct: a package/version change alone
can mark a target changed while `target_content_changed` remains false. Missing,
excluded, outside-scope, ambiguous, and unsupported targets retain their findings.

Traversal is exactly one hop. Self cycles are marked; longer cycles and downstream
impacts are explicitly not traversed. These are source facts, not behavioral impact or
inherited-field authorship. Package/reference pages use the same dataset guards and
1–100 limit as profile comparison; candidate and occurrence lists cap at 10 with
complete counts and truncation flags. For more detail, inspect the cited exact source.

## Expand or rebuild

```bash
uv run specfhir packages versions hl7.fhir.us.davinci-pas --json
uv run specfhir packages pages 'hl7.fhir.us.davinci-pas#2.1.0' --json
uv run specfhir packages list --json
```

`versions` queries the registry without changing pins. `pages` previews candidates
from the locked IG's page metadata. `list` reports installed coverage, exclusions,
reference findings, and coherence with the configuration and lock.

To add a release, edit the exact roots and publication entries in `specfhir.toml`,
then intentionally update the lock:

```bash
uv run specfhir sync --update-lock --with-validator --json
uv run specfhir check --with-validator --json
uv run pytest tests/acceptance --live-acceptance -q
```

Documentation discovery follows `ImplementationGuide.definition.page`. Prefer the
exact release's `full-ig.zip`: its embedded package archive must match the package
pin. The ZIP, selected page bytes, member paths, and public URLs are pinned.
Generated resource renderings, administrative pages, and external links are not
blindly crawled. Explicit document URLs remain available for publications without
an archive; see the configuration models in [config.py](src/specfhir/config.py).

Preparation is incremental per package/document/model identity. Existing verified
extraction and embedded-passage caches are reused; only changed inputs need work.
**Database publication still replaces the complete dataset atomically.** Readers
see the old or new dataset, never a partial publication. An unchanged sync verifies
cached inputs and skips preparation/publication.

```bash
uv run specfhir sync --rebuild --with-validator --json
uv run specfhir sync --prune-cache --with-validator --json
```

`--rebuild` forces full publication. `--prune-cache` removes recognized obsolete
prepared/embedded/vector cache generations only after successful sync. It retains
current generations, unknown files, symlinks, source archives, publications,
model downloads, and validator snapshots. These caches are disposable, not Git inputs.

If indexing fails, the previous published dataset survives; a changed lock may
still describe pending inputs, which inventory reports as a mismatch. Validator
refresh follows database publication, so a failed refresh can leave a new index
with an old service. Rerun `sync --with-validator` to finish that step.

## Validator operations

The persistent Java adapter delegates rules to pinned HL7 Validator 6.10.4.
It reuses two engines, prewarms the default context, and permits one active
validation plus eight waiting requests. Excess admission returns 429; queue waits
are bounded to ten seconds. A 150-second execution watchdog halts a stuck JVM;
Compose restarts it. Failures are returned without automatic request retries.

Normal builds use `sync --with-validator`. For validator source/image changes,
explicitly prepare and recreate the service:

```bash
uv run specfhir validator-setup --json
docker compose up -d --build --force-recreate --wait validator
```

The service snapshot tracks exact package/dependency and validator support pins,
not retrieval pages or embedding settings. A matching healthy service is reused.
Only the selected locked closure is loaded. Instance-declared profiles cannot
install extra packages; both service and client verify loaded-package evidence.

Without `--profile`, validation requests base R4; HL7 also checks `meta.profile`.
An explicit profile resolves in the same package context as retrieval. SpecFHIR
supplies no external instance repository. Missing references and incomplete
terminology coverage remain visible in the outcome.

Results separate execution, findings, and coverage. `status: "ok"` means the
operation completed, and may accompany validation errors. Inspect
`data.findings.errors`; zero errors do not establish complete conformance.
Inputs are JSON, at most 10 MiB. Returned issues are capped at 500, with full counts
and truncation reported. Read validator output is bounded to 16 MiB.

The container uses a 2 GiB heap within a 4 GiB limit. Package indexes/terminology
cache use a private Docker volume, keyed by snapshot; temporary files use a 64 MiB
tmpfs. Instances, outcomes, and request logs are not persisted by the service.
Returned findings can contain input values.

Offline is the default: HL7 prohibits network access, but Docker bridge networking
is not OS-enforced egress isolation. Optional online terminology uses a separate
process because HL7's network policy is global:

```toml
[validator]
timeout_seconds = 180
service_url = "http://127.0.0.1:55433"
online_service_url = "http://127.0.0.1:55434"
terminology_endpoint = "https://tx.fhir.org/r4"
```

```bash
SPECFHIR_TERMINOLOGY_ENDPOINT=https://tx.fhir.org/r4 \
  docker compose --profile online up -d --build --wait validator-online
uv run specfhir validate patient.json --profile USCorePatient --terminology-mode online --json
```

The endpoint must be explicitly configured and HTTPS. Recreate the online service
separately after snapshot changes. Offline acceptance does not verify online
terminology interoperability.

Validate the same instance across multiple versions by repeating `--package`:

```bash
uv run specfhir validate patient.json \
  --package 'hl7.fhir.us.core#3.1.1' \
  --package 'hl7.fhir.us.core#6.1.0' \
  --package 'hl7.fhir.us.core#7.0.0' \
  --profile USCorePatient --json
```

For independent profile selections, use `--contexts` with a JSON array. The Python
API and MCP accept the same structured `contexts` array:

```json
[
  {"package": "hl7.fhir.us.davinci-pas#2.0.1", "profile": "PASClaimInquiry"},
  {"package": "hl7.fhir.us.davinci-pas#2.1.0", "profile": "PASClaimInquiry"},
  {"package": "hl7.fhir.r4.core#4.0.1"}
]
```

Each context owns its package and optional profile. Omission selects base validation
plus the instance's declared profiles; those declarations are never stripped to make
a context succeed. Do not mix `contexts` with the single-context `package`/`profile`
arguments. Single-context convenience calls retain their original response shape;
an explicit context list always returns a matrix, including a one-entry list.

The runner executes exactly once per context, sequentially under the existing service
limits. It does not execute every possible pair. All calls receive identical JSON
semantics, including declared profiles and references; whitespace is not preserved.
No input/result persistence or automatic retries are introduced. Caller timeouts must
allow the sequential validations; terminology mode is shared by the request.

`results[]` preserves request order and contains each requested `context` and its full
bounded `result`: execution, coverage, resolved profiles, original issues, actual loaded
packages and validator snapshot identity. The first observed published dataset guards
subsequent calls. Requests allow 1–16 distinct package/profile selections; the existing
10 MiB input, 16 MiB service-response and 500-issue limits apply per context. The context
limit is an operational bound; the runner and matrix do not assume a fixed arity.

`correspondence.items[].issue_indices` uses the same column order as `results[]`.
An array contains original issue indices; `[]` means no corresponding issue in a
successfully evaluated context, while `null` means unavailable. Failures, truncation
and inconsistent snapshots are listed in `unavailable_contexts`. Successful contexts
remain comparable with status `partial`; the overall request returns an error so a
partial result cannot masquerade as full success. No available contexts means
`unavailable`. CLI execution/comparison errors take precedence over findings.

A shared finding requires a unique HL7 message ID, code, exact reported locations and
unchanged details/diagnostics. Duplicates, changed messages and missing identifiers or
locations remain `uncertain`. `shared` and `context_only` refer to available contexts;
neither implies a fixed or newly introduced defect. Group counts describe the matrix,
while aggregate findings sum original outcomes. `issues_identical` compares complete
issue arrays only when every context is available; coverage stays separate per context.
Equal issues or zero errors do not establish conformance.

For build batches, use `validate-cases cases.json --json` with a manifest such as:

```json
{"cases":[{"name":"patient","instance":"patient.json","package":"hl7.fhir.us.core#9.0.0","profile":"USCorePatient"}]}
```

Instance paths are relative to the manifest. Names must be unique; package and
profile are explicit. CLI exit codes: 0 success/warnings, 1 execution or input
failure, 2 not found/unavailable effective definition, 3 ambiguous, 4 validation
errors. Batch execution failures take precedence over error findings.

## MCP

The official Python SDK serves `contexts`, `compare`, `resolve`, `inspect`,
`search`, and `validate`
over stdio with the same structured results as the CLI. Logs use stderr.
Validation accepts JSON content, not a client filesystem path. Sync is an explicit
CLI operation; MCP has no sync tool or implicit package downloads.

Example client configuration; adjust the checkout path:

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

Complete setup before connecting. Pass `SPECFHIR_DSN` in the client environment
if using a non-default database.

## Verify

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

The default remains serial. For a bounded parallel run, pytest-xdist uses at most
four workers and keeps database-writing tests and validator-backed tests in their
own serial resource groups:

```bash
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir \
uv run pytest -n 4
```

Use `-n 0` for an explicit serial baseline. Do not use unrestricted worker counts:
the database advisory lock spans isolated schemas, and the validator intentionally
serializes its shared engines.

Database integration tests use isolated schemas with `SPECFHIR_TEST_DSN`; they
never replace the application dataset. Installed acceptance is a separate opt-in:

```bash
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir \
uv run pytest --live-acceptance -q
```

Add the two real smokes for the release-grade gate:

```bash
SPECFHIR_TEST_DSN=postgresql://specfhir@localhost:55432/specfhir \
SPECFHIR_REAL_SMOKE=1 SPECFHIR_VALIDATOR_SMOKE=1 \
uv run pytest --live-acceptance -q -o junit_family=legacy \
  --junitxml=.specfhir/acceptance.xml
```

Add `-n 4` to that command for the grouped parallel variant. Installed validation
modules stay on one worker; read-only acceptance and isolated work may overlap.

Do not run sync/build concurrently with this suite: database advisory locks span
schemas. Acceptance reuses reviewed fixtures and runs every behavioral assertion
through the API. In each pytest worker session, one result per exercised tool, status
and CLI exit class is also compared through the real CLI and MCP transports. The PAS
comparison workflow checks every direct before/after value against the
checksum-verified archive, alongside pagination, reverse comparison, and guarded
targeted inspection. Published archive examples retain full outcomes in JUnit and
assert reviewed error categories/counts. Updating those baselines requires reviewing
the new outcome, not blindly accepting changed counts.

`specfhir check` is read-only readiness, provenance, and release-scope coverage;
`--with-validator` adds service readiness, not instance validation. Pytest owns
behavioral acceptance. No second check scripting framework is required.

Existing benchmarks:

```bash
uv run python scripts/benchmark_search.py
uv run python scripts/benchmark_search.py --build
uv run python scripts/benchmark_validator.py --restart-check
```

The default search benchmark is read-only: 19 prose queries and eight exact
lookups. `--build` also measures an uncached 512-passage inference sample and
performs a full rebuild. The validator lifecycle benchmark intentionally exercises
admission and eviction; `--restart-check` interrupts/restarts the service. Run
these separately from other validation or sync work. Dated results and known
retrieval misses are in [VALIDATION.md](VALIDATION.md).
