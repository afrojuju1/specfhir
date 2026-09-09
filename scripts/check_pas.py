"""Read-only PAS acceptance; generates synthetic fixtures/results under .specfhir/."""

import asyncio
import copy
import json
import shutil
import statistics
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from mcp import Client, StdioServerParameters

from specfhir import search, validator
from specfhir.config import dsn, load
from specfhir.files import checksum
from specfhir.models import Lock
from specfhir.packages import archive_files

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "specfhir.toml"
OUTPUT = ROOT / ".specfhir/pas-acceptance"
VERSIONS = ("2.0.1", "2.1.0")


def references(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("reference"), str):
            yield obj["reference"]
        for value in obj.values():
            yield from references(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from references(value)


def clean(obj):
    """Fixture changes only: replace publication links and example identifier namespaces."""
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), dict) and "div" in obj["text"]:
            obj["text"] = {
                "status": "generated",
                "div": '<div xmlns="http://www.w3.org/1999/xhtml">Synthetic PAS fixture</div>',
            }
        for key, value in obj.items():
            if (
                key == "system"
                and isinstance(value, str)
                and value.startswith("http://example.org/")
            ):
                obj[key] = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, value))
            else:
                clean(value)
    elif isinstance(obj, list):
        for value in obj:
            clean(value)


def fixture(instance, resources, version, response):
    result = copy.deepcopy(instance)
    seen = {e["resource"]["resourceType"] + "/" + e["resource"]["id"] for e in result["entry"]}
    # Add the published resources required by relative references, preserving one consistent base.
    for entry in result["entry"]:
        for ref in references(entry):
            if ref not in seen and ref in resources:
                seen.add(ref)
                result["entry"].append(
                    {
                        "fullUrl": "http://example.org/fhir/" + ref,
                        "resource": copy.deepcopy(resources[ref]),
                    }
                )
    clean(result)
    if version == "2.0.1" and response:
        # This fixture omits optional pricing; the raw example retains offline currency errors.
        for ext in result["entry"][0]["resource"]["item"][0]["extension"]:
            if ext["url"].endswith("extension-itemAuthorizedDetail"):
                ext["extension"] = [e for e in ext["extension"] if e["url"] != "unitPrice"]
    return result


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock = Lock.model_validate_json((ROOT / "specfhir.lock").read_bytes())
    report, cases, baselines = {}, [], {}
    # Evict PAS engines to measure real cold-context loading, then retain both PAS contexts.
    for package in ("hl7.fhir.r4.core#4.0.1", "hl7.fhir.us.core#9.0.0"):
        assert validator.validate({"resourceType": "Patient"}, package=package).status == "ok"
    inquiry = None
    for version in VERSIONS:
        package = f"hl7.fhir.us.davinci-pas#{version}"
        pin = next(p for p in lock.packages if p.key == package)
        archive = ROOT / ".specfhir/packages" / f"{package}.tgz"
        assert checksum(archive) == pin.sha256
        resources = [
            json.loads(b)
            for n, b in archive_files(archive)
            if n.startswith("package/example/") and n.endswith(".json")
        ]
        lookup = {r["resourceType"] + "/" + r["id"]: r for r in resources if "id" in r}
        if inquiry is None:
            inquiry = copy.deepcopy(lookup["Claim/PASClaimInquiryExample"])
            inquiry.pop("identifier")
            clean(inquiry)
        element = search.resolve("PASClaimInquiry.identifier", package=package)
        assert element.status == "ok" and element.data
        assert element.data["source"]["artifact_version"] == version
        assert element.data["element"]["min"] == (0 if version == "2.0.1" else 1)
        other = "2.1.0" if version == "2.0.1" else "2.0.1"
        canonical = element.data["source"]["canonical"]
        assert search.resolve(canonical + "|" + other, package=package).status == "not_found"
        for query in (
            "submission of a prior authorization request",
            "authorization inquiry",
            "cancel authorization",
        ):
            for mode in ("lexical", "hybrid"):
                found = search.search(
                    query, package=package, resource_type="Documentation", mode=mode
                )
                assert found.data and found.data["results"], query
                assert all(r["source"]["package"] == package for r in found.data["results"])
        report[version] = {"archive_sha256": pin.sha256, "examples": {}}
        for label, name, profile in (
            ("request", "ReferralAuthorizationBundleExample", "PASRequestBundle"),
            ("response", "ReferralAuthorizationResponseBundleExample", "PASResponseBundle"),
        ):
            original = lookup["Bundle/" + name]
            started = time.perf_counter()
            raw = validator.validate(original, package=package, profile=profile)
            assert raw.status == "ok", raw.message
            elapsed = time.perf_counter() - started
            good = fixture(original, lookup, version, label == "response")
            actual = validator.validate(good, package=package, profile=profile)
            assert actual.status == "ok" and actual.data, actual.message
            assert actual.data["findings"]["errors"] == 0, actual.data["issues"]
            assert package in actual.data["loaded_packages"]
            assert f"hl7.fhir.us.davinci-pas#{other}" not in actual.data["loaded_packages"]
            assert actual.data["dependency_resolutions"] == [
                {
                    "package": "hl7.fhir.uv.subscriptions-backport.r4#1.1.0",
                    "declared": "hl7.fhir.r4.core#4.0.0",
                    "selected": "hl7.fhir.r4.core#4.0.1",
                    "reason": "Explicit R4 core selection",
                }
            ]
            path = OUTPUT / f"{version}-{label}.json"
            path.write_text(json.dumps(good, indent=2) + "\n")
            cases.append(
                {
                    "name": f"{version}-{label}",
                    "instance": path.name,
                    "package": package,
                    "profile": profile,
                }
            )
            measurements = []
            for _ in range(5):
                started = time.perf_counter()
                assert validator.validate(good, package=package, profile=profile) == actual
                measurements.append(1000 * (time.perf_counter() - started))
            report[version]["examples"][label] = {
                "first_request_seconds": round(elapsed, 3),
                "warm_median_ms": round(statistics.median(measurements), 2),
                "published_result": raw.model_dump(exclude_none=True),
                "synthetic_result": actual.model_dump(exclude_none=True),
            }
            if label == "request":
                bad = copy.deepcopy(good)
                del bad["entry"][0]["resource"]["patient"]
                invalid = validator.validate(bad, package=package, profile=profile)
                assert invalid.status == "ok" and invalid.data["findings"]["errors"] > 0
                report[version]["invalid_request"] = invalid.model_dump(exclude_none=True)
                baselines[version] = (good, actual.model_dump(exclude_none=True))
        print(f"PAS {version}: retrieval, request/response, invalid variant passed", flush=True)
    health_url = load(CONFIG).validator.service_url + "/health"
    warm_builds = httpx.get(health_url, trust_env=False, timeout=5).json()["engine_builds"]
    # The identical input must consistently acquire only the newer release's required-id finding.
    for version in (*VERSIONS, *VERSIONS):
        result = validator.validate(
            inquiry, package=f"hl7.fhir.us.davinci-pas#{version}", profile="PASClaimInquiry"
        )
        assert result.status == "ok" and result.data
        has_required_id = any(
            "Claim.identifier: minimum required = 1" in json.dumps(i) for i in result.data["issues"]
        )
        assert has_required_id == (version == "2.1.0")
        report[version]["inquiry_without_identifier"] = result.model_dump(exclude_none=True)
    manifest = OUTPUT / "cases.json"
    manifest.write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    uv = shutil.which("uv")
    assert uv
    cli = json.loads(
        subprocess.check_output(
            [uv, "run", "--no-sync", "specfhir", "validate-cases", str(manifest), "--json"],
            cwd=ROOT,
        )
    )
    assert cli["data"]["findings"]["errors"] == 0
    for case in cli["data"]["cases"]:
        version, label = case["name"].split("-", 1)
        assert case["result"] == report[version]["examples"][label]["synthetic_result"]

    async def parity():
        params = StdioServerParameters(
            command=uv,
            args=["run", "--no-sync", "specfhir", "mcp"],
            cwd=ROOT,
            env={"SPECFHIR_DSN": dsn()},
        )
        async with Client(params, read_timeout_seconds=180) as client:
            for version, (good, expected) in baselines.items():
                response = await client.call_tool(
                    "validate",
                    {
                        "instance": good,
                        "package": f"hl7.fhir.us.davinci-pas#{version}",
                        "profile": "PASRequestBundle",
                    },
                )
                assert response.structured_content == expected

    asyncio.run(parity())
    health = httpx.get(health_url, trust_env=False, timeout=5).json()
    assert health["engine_builds"] == warm_builds
    assert set(health["cached_contexts"]) == {f"hl7.fhir.us.davinci-pas#{v}" for v in VERSIONS}
    report["checks"] = {
        "both_contexts_remain_warm": True,
        "version_isolation": True,
        "cli_api_mcp_parity": True,
        "build_manifest": str(manifest),
    }
    (OUTPUT / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PAS acceptance passed; report:", OUTPUT / "results.json")


if __name__ == "__main__":
    run()
