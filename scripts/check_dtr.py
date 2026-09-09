"""DTR acceptance against the local index and HL7 service; evidence stays ignored."""

import asyncio
import copy
import json
import subprocess
from pathlib import Path

from mcp import Client, StdioServerParameters

from specfhir import packages, search, validator
from specfhir.config import dsn
from specfhir.files import checksum
from specfhir.models import Lock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "hl7.fhir.us.davinci-dtr#2.2.0"
PROFILE = "DTRStdQuestionnaire"
OUTPUT = ROOT / ".specfhir/dtr-acceptance"


def run():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock = Lock.model_validate_json((ROOT / "specfhir.lock").read_bytes())
    pin = next(p for p in lock.packages if p.key == PACKAGE)
    archive = ROOT / ".specfhir/packages" / f"{PACKAGE}.tgz"
    assert checksum(archive) == pin.sha256
    element = search.resolve(f"{PROFILE}.subjectType", package=PACKAGE)
    assert element.status == "ok" and element.data
    assert element.data["source"]["package"] == PACKAGE
    assert element.data["source"]["artifact_version"] == "2.2.0"
    assert element.data["element"]["min"] == 1
    canonical = element.data["source"]["canonical"]
    assert search.resolve(canonical + "|2.0.1", package=PACKAGE).status == "not_found"
    operation = search.resolve("QuestionnairePackage", package=PACKAGE)
    assert operation.status == "ok" and operation.data
    assert operation.data["source"]["package"] == PACKAGE
    inspection = search.inspect(PROFILE, package=PACKAGE)
    assert inspection.status == "ok" and inspection.data
    good = {
        "resourceType": "Questionnaire",
        "id": "synthetic-dtr",
        "url": "https://example.org/Questionnaire/synthetic-dtr",
        "name": "SyntheticDTR",
        "title": "Synthetic DTR Questionnaire",
        "status": "draft",
        "subjectType": ["Patient"],
        "text": {
            "status": "generated",
            "div": '<div xmlns="http://www.w3.org/1999/xhtml">Synthetic questionnaire</div>',
        },
        "item": [{"linkId": "1", "type": "string", "text": "Supporting information"}],
    }
    valid = validator.validate(good, package=PACKAGE, profile=PROFILE)
    assert valid.status == "ok" and valid.data, valid.message
    assert valid.data["findings"]["errors"] == 0, valid.data["issues"]
    assert PACKAGE in valid.data["loaded_packages"]
    assert "hl7.fhir.us.davinci-pas#2.2.1" in valid.data["loaded_packages"]
    assert "hl7.fhir.us.davinci-pas#2.0.1" not in valid.data["loaded_packages"]
    bad = copy.deepcopy(good)
    del bad["subjectType"]
    invalid = validator.validate(bad, package=PACKAGE, profile=PROFILE)
    assert invalid.status == "ok" and invalid.data
    assert any(
        "Questionnaire.subjectType: minimum required = 1" in json.dumps(issue)
        for issue in invalid.data["issues"]
    ), invalid.data["issues"]
    core = validator.validate(bad, package="hl7.fhir.r4.core#4.0.1")
    assert core.status == "ok" and core.data and core.data["findings"]["errors"] == 0
    published = {}
    for name, raw in packages.archive_files(archive):
        if name.startswith("package/example/Questionnaire-") and name.endswith(".json"):
            result = validator.validate(json.loads(raw), package=PACKAGE)
            assert result.status == "ok" and result.data, result.message
            published[name] = result.model_dump(exclude_none=True)
    retrieval = {}
    for query in ("questionnaire package", "CQL", "adaptive questionnaire"):
        for mode in ("lexical", "hybrid"):
            result = search.search(query, package=PACKAGE, resource_type="Documentation", mode=mode)
            assert result.data and result.data["results"], (query, mode)
            assert all(r["source"]["package"] == PACKAGE for r in result.data["results"])
            retrieval[f"{mode}:{query}"] = result.model_dump(exclude_none=True)
    instance_path = OUTPUT / "questionnaire.json"
    instance_path.write_text(json.dumps(good, indent=2) + "\n")
    cli = json.loads(
        subprocess.check_output(
            [
                "uv",
                "run",
                "specfhir",
                "validate",
                str(instance_path),
                "--package",
                PACKAGE,
                "--profile",
                PROFILE,
                "--json",
            ],
            cwd=ROOT,
        )
    )
    assert cli == valid.model_dump(exclude_none=True)

    async def parity():
        params = StdioServerParameters(
            command="uv",
            args=["run", "--no-sync", "specfhir", "mcp"],
            cwd=ROOT,
            env={"SPECFHIR_DSN": dsn()},
        )
        async with Client(params, read_timeout_seconds=180) as client:
            for tool, args, expected in (
                ("resolve", {"selector": "QuestionnairePackage", "package": PACKAGE}, operation),
                ("inspect", {"selector": PROFILE, "package": PACKAGE}, inspection),
                ("validate", {"instance": good, "package": PACKAGE, "profile": PROFILE}, valid),
            ):
                response = await client.call_tool(tool, args)
                assert response.structured_content == expected.model_dump(exclude_none=True)
            response = await client.call_tool(
                "search",
                {
                    "query": "CQL",
                    "package": PACKAGE,
                    "resource_type": "Documentation",
                    "mode": "hybrid",
                },
            )
            assert response.structured_content == retrieval["hybrid:CQL"]

    asyncio.run(parity())
    inventory = packages.inventory(ROOT / "specfhir.toml")
    assert inventory["index_matches_lock"] and inventory["config_matches_lock"]
    assert inventory["validator"]["matches_lock"] and inventory["validator"]["ready"]
    report = {
        "package": PACKAGE,
        "archive_sha256": pin.sha256,
        "valid": valid.model_dump(exclude_none=True),
        "invalid": invalid.model_dump(exclude_none=True),
        "core": core.model_dump(exclude_none=True),
        "published_examples": published,
        "retrieval": retrieval,
        "checks": {"cli_api_mcp_parity": True, "inventory_coherent": True},
    }
    (OUTPUT / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("DTR acceptance passed:", OUTPUT / "results.json")


if __name__ == "__main__":
    run()
