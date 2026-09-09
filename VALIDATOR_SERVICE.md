# Persistent validator service

SpecFHIR uses HL7's pinned validation engine in a long-lived Compose service.
The service owns engine lifetime; Python owns profile resolution and result formatting.
It implements no FHIR rules.

## Decisions

- Keep exact package snapshots, identified by the retrieval lock and support-package checksums.
  Prepare and verify once during explicit setup. A running service never changes its snapshot;
  a changed index is rejected until setup and service recreation complete.
- Reuse HL7 `ValidationService` engine construction and `ValidationEngine` validation directly.
  The upstream web wrapper accepts mutable engine settings, shares session state, and does not
  expose the loaded-package evidence required by our contract. A small Java HTTP adapter
  restricts inputs and exposes this evidence through the engine API.
- Prewarm the default context before readiness. Cache at most two contexts in LRU order.
  Requests never select arbitrary packages, terminology servers, or engine options.
- Serialize engine use because HL7 engines contain mutable context/cache state. Admit at most
  nine requests (one executing, eight waiting); reject excess work with 429. Queue waits are
  bounded to ten seconds. Health checks remain independently available.
- A hard execution deadline terminates a stuck JVM; Compose restarts it and prewarms again.
  Java interruption alone cannot guarantee that an in-process validation stops. No automatic
  validation retries: callers must see timeout/unavailable results, not hidden duplicate work.
- The Java service publishes port 8080 directly on host loopback port 55433 through
  Docker’s normal bridge network. Offline mode uses HL7’s prohibited-network policy;
  it is not OS-enforced egress isolation. No reverse proxy is needed.
  Optional online terminology uses a separate process and an operator-set
  endpoint; the global HL7 network policy can never be toggled by a validation request.
- No submitted instances, results, or request logs are persisted. Requests stay in memory;
  writable package indexes and terminology cache live in a private Docker volume
  under the snapshot identity. The PAS graph expands beyond 3 GB, so this cache must
  not consume the JVM memory budget. Temporary files use a 64 MiB tmpfs.

## Acceptance

Verify unchanged findings through CLI/API/MCP, repeated warm validation, profile/context
isolation, concurrent submissions, stale snapshot rejection, missing service behavior,
restart recovery, bounded queues/deadlines, and offline mode enforcement. Record measured
cold startup and warm latency. Java installation on the host is no longer needed.

## Measured locally (2026-09-09)

Direct Java port, synthetic US Core Patient, ten repeated warm requests: median
13.17 ms, maximum 16.47 ms. Six concurrent requests preserved identical outcomes;
excess admission returned 429 in 43.54 ms. Changing profiles did not contaminate the
next request. Three contexts exercised the two-engine limit and eviction. A forced
one-second cold-engine deadline terminated the JVM; Compose restarted it and the
default engine became ready again in 27.20 seconds with unchanged findings.
These are local lifecycle checks, not a general FHIR throughput benchmark.

Full regression: 10 tests passed in 89.76 seconds with real package and validator
smokes enabled, including CLI/API/MCP checks. Ruff, Pyright, formatting, Compose
configuration, and source/wheel builds passed. Online terminology was not tested.

## PAS package cache

The side-by-side PAS graph expands to approximately 3.6 GB before validator support
packages. `/cache` is an anonymous Docker volume owned by UID 10001, separated by
snapshot identity; `/work` remains a 64 MiB tmpfs. Startup verifies source files and
reuses matching cache files. This cache may survive container recreation and stores
package indexes/terminology data, never submitted instances or validation results.
To discard an unused validator cache deliberately, remove its stopped container with
`docker compose rm -s -v validator`, then start it again from the prepared snapshot.

`sync --with-validator` reuses a ready service with the expected snapshot. It does
not rebuild warm engines merely because another build ran.
