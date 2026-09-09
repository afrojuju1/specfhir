"""Exercise the live service. --restart-check intentionally restarts its JVM."""

import argparse
import concurrent.futures
import json
import socket
import statistics
import subprocess
import time
from pathlib import Path

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--restart-check", action="store_true")
args = parser.parse_args()
url = "http://127.0.0.1:55433"
client = httpx.Client(base_url=url, trust_env=False, timeout=170)


def health():
    response = client.get("/health")
    response.raise_for_status()
    return response.json()


def wait_ready():
    started = time.perf_counter()
    while time.perf_counter() - started < 180:
        try:
            return health(), time.perf_counter() - started
        except httpx.HTTPError:
            time.sleep(1)
    raise AssertionError("Service did not recover")


initial = health()
base = {
    "snapshot_id": initial["snapshot_id"],
    "package": "hl7.fhir.us.core#9.0.0",
    "instance": json.dumps({"resourceType": "Patient", "id": "synthetic-service-check"}),
    "profile": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient|9.0.0",
    "terminology_mode": "offline",
    "timeout_seconds": 150,
}


def validate(payload):
    response = client.post("/validate", json=payload)
    response.raise_for_status()
    return response.json()


# A request-specific profile must not alter the next request on the same engine.
expected = validate(base)
assert sum(i["severity"] == "error" for i in expected["outcome"]["issue"]) == 2
unprofiled = validate({**base, "profile": None})
assert not any(i["severity"] in {"error", "fatal"} for i in unprofiled["outcome"].get("issue", []))
assert validate(base) == expected
before = health()["engine_builds"]
durations = []
for _ in range(10):
    start = time.perf_counter()
    assert validate(base) == expected
    durations.append(1000 * (time.perf_counter() - start))
assert health()["engine_builds"] == before
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    results = list(pool.map(validate, [base] * 6))
assert all(r == expected for r in results)
assert client.post("/validate", json={**base, "snapshot_id": "0" * 64}).status_code == 409
assert client.post("/validate", json={**base, "terminology_mode": "online"}).status_code == 409
# Hold nine admitted request bodies open: a tenth must fail fast while health remains live.
sockets = []
try:
    for _ in range(9):
        sock = socket.create_connection(("127.0.0.1", 55433), timeout=5)
        sockets.append(sock)
        sock.sendall(
            b"POST /validate HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nContent-Length: 1024\r\n\r\n{"
        )
    until = time.monotonic() + 5
    while health()["admitted"] < 9 and time.monotonic() < until:
        time.sleep(0.1)
    assert health()["admitted"] == 9
    start = time.perf_counter()
    assert client.post("/validate", json=base).status_code == 429
    overload_ms = 1000 * (time.perf_counter() - start)
    assert overload_ms < 1000
finally:
    for sock in sockets:
        sock.close()
    until = time.monotonic() + 25
    while health()["admitted"] and time.monotonic() < until:
        time.sleep(0.1)
assert health()["admitted"] == 0
assert validate(base) == expected

# Build a third context and verify bounded LRU eviction and preserved findings on return.
for package in ("hl7.fhir.r4.core#4.0.1", "hl7.fhir.uv.extensions.r4#5.3.0"):
    validate({**base, "package": package, "profile": None})
assert len(health()["cached_contexts"]) == 2
assert health()["evictions"] >= 1
assert validate(base) == expected
report = {
    "warm_median_ms": round(statistics.median(durations), 2),
    "warm_max_ms": round(max(durations), 2),
    "concurrent_requests": 6,
    "overload_rejection_ms": round(overload_ms, 2),
    "health": health(),
}
if args.restart_check:
    # A cold context cannot finish loading in a one-second budget. The watchdog must kill it.
    context = next(
        k
        for k in ("hl7.fhir.r4.core#4.0.1", "hl7.fhir.uv.extensions.r4#1.0.0")
        if k not in health()["cached_contexts"]
    )
    name = subprocess.check_output(
        ["docker", "compose", "ps", "-q", "validator"], text=True
    ).strip()

    def restarts():
        return int(
            subprocess.check_output(
                ["docker", "inspect", name, "--format", "{{.RestartCount}}"], text=True
            )
        )

    previous = restarts()
    try:
        client.post(
            "/validate", json={**base, "package": context, "profile": None, "timeout_seconds": 1}
        )
    except (httpx.RemoteProtocolError, httpx.ReadError):
        pass
    else:
        raise AssertionError("Watchdog should terminate the connection with the JVM")
    recovered, seconds = wait_ready()
    assert restarts() > previous
    assert recovered["engine_builds"] == 1
    assert validate(base) == expected
    report["watchdog_recovery_seconds"] = round(seconds, 2)
    report["restart_count_increased"] = True
Path(".specfhir/validator-service-benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
