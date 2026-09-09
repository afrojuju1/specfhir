"""CRD acceptance against the local index/service; generated evidence stays in .specfhir."""

import copy
import json
import statistics
import subprocess
import time
from pathlib import Path

from specfhir import packages, search, validator
from specfhir.files import checksum
from specfhir.models import Lock

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "specfhir.toml"
OUTPUT = ROOT / ".specfhir/crd-acceptance"
PACKAGE = "hl7.fhir.us.davinci-crd#2.2.1"


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock = Lock.model_validate_json((ROOT / "specfhir.lock").read_bytes())
    pin = next(p for p in lock.packages if p.key == PACKAGE)
    archive = ROOT / ".specfhir/packages" / f"{PACKAGE}.tgz"
    assert checksum(archive) == pin.sha256
    examples = {
        name: json.loads(raw)
        for name, raw in packages.archive_files(archive)
        if name.startswith("package/example/") and name.endswith(".json")
    }
    report = {"package": PACKAGE, "archive_sha256": pin.sha256, "cases": {}}
    cases = []
    for kind, required in (("DeviceRequest", "status"), ("ServiceRequest", "authoredOn")):
        profile = "CRD" + kind
        element = search.resolve(f"{profile}.{required}", package=PACKAGE)
        assert element.status == "ok" and element.data
        assert element.data["source"]["package"] == PACKAGE
        assert element.data["source"]["artifact_version"] == "2.2.1"
        assert element.data["element"]["min"] == 1
        canonical = element.data["source"]["canonical"]
        assert search.resolve(canonical + "|2.1.0", package=PACKAGE).status == "not_found"
        started = time.perf_counter()
        original = validator.validate(
            examples[f"package/example/{kind}-example.json"], package=PACKAGE, profile=profile
        )
        assert original.status == "ok", original.message
        first_seconds = time.perf_counter() - started
        # Independent synthetic fixture: no edits or relaxed flags applied to published examples.
        instance = {
            "resourceType": kind,
            "id": "synthetic",
            "text": {
                "status": "generated",
                "div": '<div xmlns="http://www.w3.org/1999/xhtml">Synthetic CRD order</div>',
            },
            "status": "draft",
            "intent": "order",
            "contained": [
                {
                    "resourceType": "Patient",
                    "id": "patient",
                    "identifier": [
                        {
                            "system": "urn:uuid:ba51bc63-6b10-411c-a7df-d48005b91018",
                            "value": "patient",
                        }
                    ],
                    "name": [{"family": "Synthetic", "given": ["Example"]}],
                    "gender": "unknown",
                    "birthDate": "1980-01-01",
                },
                {
                    "resourceType": "Practitioner",
                    "id": "practitioner",
                    "identifier": [
                        {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567893"}
                    ],
                    "name": [{"family": "Synthetic", "given": ["Example"]}],
                },
            ],
            "subject": {"reference": "#patient"},
            "requester": {"reference": "#practitioner"},
            "authoredOn": "2026-01-01",
            "codeCodeableConcept" if kind == "DeviceRequest" else "code": {
                "text": "Synthetic order for validation"
            },
        }
        valid = validator.validate(instance, package=PACKAGE, profile=profile)
        assert valid.status == "ok" and valid.data, valid.message
        assert valid.data["findings"]["errors"] == 0, valid.data["issues"]
        assert PACKAGE in valid.data["loaded_packages"]
        assert not any("davinci-pas#" in key for key in valid.data["loaded_packages"])
        invalid = copy.deepcopy(instance)
        del invalid[required]
        bad = validator.validate(invalid, package=PACKAGE, profile=profile)
        assert bad.status == "ok" and bad.data
        assert any(
            f"{kind}.{required}: minimum required = 1" in json.dumps(issue)
            for issue in bad.data["issues"]
        ), bad.data["issues"]
        core = validator.validate(invalid, package="hl7.fhir.r4.core#4.0.1")
        assert core.status == "ok" and core.data["findings"]["errors"] == 0
        measurements = []
        for _ in range(3):
            started = time.perf_counter()
            assert validator.validate(instance, package=PACKAGE, profile=profile) == valid
            measurements.append(1000 * (time.perf_counter() - started))
        path = OUTPUT / f"{kind}.json"
        path.write_text(json.dumps(instance, indent=2) + "\n")
        cases.append({"name": kind, "instance": path.name, "package": PACKAGE, "profile": profile})
        report["cases"][kind] = {
            "published_result": original.model_dump(exclude_none=True),
            "synthetic_result": valid.model_dump(exclude_none=True),
            "invalid_result": bad.model_dump(exclude_none=True),
            "core_result": core.model_dump(exclude_none=True),
            "first_request_seconds": round(first_seconds, 3),
            "warm_median_ms": round(statistics.median(measurements), 2),
        }
        print(f"CRD {kind}: valid, missing {required}, core comparison passed", flush=True)
    for query in ("order-sign", "coverage information system action", "prefetch"):
        for mode in ("lexical", "hybrid"):
            result = search.search(query, package=PACKAGE, resource_type="Documentation", mode=mode)
            assert result.data and result.data["results"], (query, mode)
            assert all(r["source"]["package"] == PACKAGE for r in result.data["results"])
    manifest = OUTPUT / "cases.json"
    manifest.write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    cli = json.loads(
        subprocess.check_output(
            ["uv", "run", "specfhir", "validate-cases", str(manifest), "--json"], cwd=ROOT
        )
    )
    assert cli["data"]["findings"]["errors"] == 0
    for case in cli["data"]["cases"]:
        assert case["result"] == report["cases"][case["name"]]["synthetic_result"]
    inventory = packages.inventory(CONFIG)
    assert inventory["index_matches_lock"] and inventory["config_matches_lock"]
    assert inventory["validator"]["matches_lock"] and inventory["validator"]["ready"]
    report["checks"] = {
        "workflow_retrieval": True,
        "cli_api_parity": True,
        "inventory_coherent": True,
    }
    (OUTPUT / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("CRD acceptance passed:", OUTPUT / "results.json")


if __name__ == "__main__":
    run()
