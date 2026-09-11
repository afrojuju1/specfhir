"""Reference findings use source-package scope and publish with the dataset."""

import json

import pytest
from helpers import archive, profile
from typer.testing import CliRunner

from specfhir import db, index, references, search
from specfhir.cli import app


def test_reference_findings_and_atomicity(tmp_path, database, monkeypatch):
    config = tmp_path / "specfhir.toml"
    config.write_text(
        'packages=["example.root#1.0.0","example.other#1.0.0"]\ndefault_package="example.root#1.0.0"\n'
    )
    cache = tmp_path / ".specfhir/packages"
    common = "https://example.org/Common"
    first = profile("Common", version="1.0.0")
    second = profile("Common", version="2.0.0")
    archive(cache, "example.dep#1.0.0", [first])
    archive(cache, "example.dep#2.0.0", [second])
    archive(cache, "example.bridge#1.0.0", [], deps={"example.dep": "2.0.0"})
    archive(cache, "example.r5#1.0.0", [profile("Excluded")], release="5.0.0")
    archive(cache, "example.other#1.0.0", [profile("Outside")])
    child = profile("Child", baseDefinition=common)
    child["snapshot"]["element"][1].update(
        type=[
            {
                "code": "Reference",
                "profile": [common + "|1.0.0"],
                "targetProfile": [
                    common + "|9.0.0",
                    "https://example.org/Outside",
                    "https://example.org/Excluded",
                ],
            }
        ],
        binding={"valueSet": "https://example.org/Missing"},
        contentReference="#Patient",
    )
    child["snapshot"]["element"][2]["contentReference"] = "#Absent"
    child["snapshot"]["element"][3]["binding"] = {"valueSet": "#contained"}
    nested = profile("Nested", baseDefinition=common)
    # An artifact in a dependency must not inherit the requesting root's extra dependencies.
    archive(cache, "example.owner#1.0.0", [nested], deps={"example.dep": "1.0.0"})
    own = profile("Own", baseDefinition="https://example.org/Own")
    archive(cache, "example.shadow#1.0.0", [own])
    valueset = {
        "resourceType": "ValueSet",
        "id": "Imports",
        "url": "https://example.org/Imports",
        "compose": {
            "include": [{"valueSet": ["https://example.org/Imports"]}],
            "exclude": [{"valueSet": ["https://example.org/Missing"]}],
        },
    }
    archive(
        cache,
        "example.root#1.0.0",
        [child, own, valueset, {"resourceType": "CodeSystem", "id": "NoCanonical"}],
        deps={
            "example.dep": "1.0.0",
            "example.bridge": "1.0.0",
            "example.r5": "1.0.0",
            "example.owner": "1.0.0",
            "example.shadow": "1.0.0",
        },
    )
    first_sync = index.sync(config)
    assert first_sync["reference_checks"]["status"] == "completed"
    assert set(first_sync["reference_checks"]["counts"]) == {
        "resolved",
        "ambiguous",
        "not_found_in_scope",
        "outside_scope",
        "excluded",
        "unsupported",
    }
    result = search.inspect("Child", config_path=config)
    assert result.status == "ok" and result.data
    findings = {r["target"]: r for r in result.data["references"]["items"]}
    assert findings[common]["status"] == "ambiguous"
    assert findings[common + "|1.0.0"]["status"] == "resolved"
    assert findings[common + "|9.0.0"]["status"] == "not_found_in_scope"
    assert findings["https://example.org/Outside"]["status"] == "outside_scope"
    assert findings["https://example.org/Excluded"]["status"] == "excluded"
    assert "5.0.0" in findings["https://example.org/Excluded"]["detail"]["candidates"][0]["reason"]
    assert findings["#Patient"]["status"] == "resolved"
    assert findings["#Absent"]["status"] == "not_found_in_scope"
    assert findings["#contained"]["status"] == "unsupported"
    assert search.resolve(common, package="example.root#1.0.0").status == "ambiguous"
    assert search.resolve(common + "|1.0.0", package="example.root#1.0.0").status == "ok"
    assert search.inspect("Nested", config_path=config).data["references"]["counts"] == {
        "resolved": 1
    }
    assert search.inspect("Own", config_path=config).data["references"]["counts"] == {"resolved": 1}
    assert search.inspect("Imports", config_path=config).data["references"]["counts"] == {
        "resolved": 1,
        "not_found_in_scope": 1,
    }
    incoming = search.inspect(common + "|1.0.0", view="incoming", limit=1, config_path=config)
    reverse = incoming.data["incoming_references"]
    assert incoming.data["source"]["artifact_version"] == "1.0.0"
    assert reverse["total"] == 2 and reverse["next_offset"] == 1
    assert reverse["counts"] == {"baseDefinition": 1, "profile": 1}
    page = search.inspect(
        common + "|1.0.0",
        view="incoming",
        offset=1,
        limit=1,
        dataset_id=incoming.dataset_id,
        config_path=config,
    )
    items = reverse["items"] + page.data["incoming_references"]["items"]
    assert [
        (item["source"]["resource_id"], item["relationship"], item["target"]) for item in items
    ] == [
        ("Child", "profile", common + "|1.0.0"),
        ("Nested", "baseDefinition", common),
    ]
    assert "contentReference" not in reverse["coverage"]["relationships"]
    assert (
        search.inspect(
            common + "|2.0.0", view="incoming", package="example.root#1.0.0", config_path=config
        ).data["incoming_references"]["total"]
        == 0
    )
    assert search.inspect(common, view="incoming", config_path=config).status == "ambiguous"
    assert (
        search.inspect(
            "Outside", view="incoming", package="example.other#1.0.0", config_path=config
        ).data["incoming_references"]["total"]
        == 0
    )
    assert (
        search.inspect("NoCanonical", view="incoming", config_path=config).data[
            "incoming_references"
        ]["status"]
        == "not_checked"
    )
    with pytest.raises(ValueError, match="dataset changed|Published dataset changed"):
        search.inspect(common + "|1.0.0", view="incoming", dataset_id="stale", config_path=config)
    cli = CliRunner().invoke(
        app,
        [
            "inspect",
            common + "|1.0.0",
            "--view",
            "incoming",
            "--limit",
            "1",
            "--config",
            str(config),
            "--json",
        ],
    )
    assert cli.exit_code == 0
    assert json.loads(cli.stdout) == incoming.model_dump(exclude_none=True)
    assert index.sync(config)["reference_checks"] == first_sync["reference_checks"]
    # A failure during reference publication rolls back the replacement artifacts and findings.
    old = references.publish
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)

    def fail(*args):
        raise ValueError("reference publication failed")

    monkeypatch.setattr(references, "publish", fail)
    with pytest.raises(ValueError, match="reference publication failed"):
        index.sync(config)
    assert search.inspect("Child", config_path=config) == result
    monkeypatch.setattr(references, "publish", old)
    rebuilt = index.sync(config)
    assert rebuilt["reference_checks"]["counts"] == first_sync["reference_checks"]["counts"]
    with db.connect() as conn:
        persisted = conn.execute("SELECT count(*) AS n FROM artifact_references").fetchone()["n"]
        assert persisted == sum(rebuilt["reference_checks"]["counts"].values())


def test_local_reference_without_snapshot_and_bounded_inspection(tmp_path, database):
    config = tmp_path / "specfhir.toml"
    config.write_text('packages=["example#1.0.0"]\ndefault_package="example#1.0.0"\n')
    partial = profile(
        "Partial",
        snapshot={},
        differential={
            "element": [{"id": "Patient", "path": "Patient", "contentReference": "#Inherited"}]
        },
    )
    large = profile("Large")
    large["snapshot"]["element"][0]["type"] = [
        {
            "code": "Reference",
            "targetProfile": [f"https://example.org/missing/{i}" for i in range(105)],
        }
    ]
    urn = {"resourceType": "ValueSet", "id": "Oid", "url": "urn:oid:1.2.3"}
    large["snapshot"]["element"][0]["binding"] = {"valueSet": "urn:oid:1.2.3"}
    archive(tmp_path / ".specfhir/packages", "example#1.0.0", [partial, large, urn])
    index.sync(config)
    assert search.resolve("urn:oid:1.2.3", config_path=config).status == "ok"
    result = search.inspect("Partial", view="raw", config_path=config)
    assert result.data["references"]["counts"] == {"unsupported": 1}
    result = search.inspect("Large", config_path=config)
    findings = result.data["references"]
    assert findings["counts"] == {"not_found_in_scope": 105, "resolved": 1}
    assert len(findings["items"]) == 100 and findings["truncated"]


def test_capability_and_operation_relationships(tmp_path, database):
    config = tmp_path / "specfhir.toml"
    config.write_text('packages=["example#1.0.0"]\ndefault_package="example#1.0.0"\n')
    input_profile = profile("Input")
    output_profile = profile("Output")
    value_set = {"resourceType": "ValueSet", "id": "Codes", "url": "https://example.org/Codes"}
    base = {
        "resourceType": "OperationDefinition",
        "id": "BaseOperation",
        "url": "https://example.org/BaseOperation",
    }
    operation = {
        "resourceType": "OperationDefinition",
        "id": "Operation",
        "url": "https://example.org/Operation",
        "base": base["url"],
        "inputProfile": input_profile["url"],
        "outputProfile": output_profile["url"],
        "parameter": [
            {
                "targetProfile": [input_profile["url"]],
                "binding": {"valueSet": value_set["url"]},
                "part": [
                    {
                        "targetProfile": [output_profile["url"]],
                        "binding": {"valueSet": "https://example.org/Missing"},
                    }
                ],
            }
        ],
    }
    capability = {
        "resourceType": "CapabilityStatement",
        "id": "Capability",
        "url": "https://example.org/Capability",
        "rest": [
            {
                "resource": [
                    {
                        "profile": input_profile["url"],
                        "supportedProfile": [output_profile["url"]],
                        "operation": [{"definition": operation["url"]}],
                    }
                ],
                "operation": [
                    {"definition": "OperationDefinition/BaseOperation"},
                    {"definition": "https://example.org/MissingOperation"},
                ],
            }
        ],
    }
    archive(
        tmp_path / ".specfhir/packages",
        "example#1.0.0",
        [input_profile, output_profile, value_set, base, operation, capability],
    )
    report = index.sync(config)
    assert {
        "CapabilityStatement",
        "OperationDefinition",
    } <= set(report["reference_checks"]["resource_types"])

    operation_refs = search.inspect("Operation", config_path=config).data["references"]
    assert operation_refs["counts"] == {"resolved": 6, "not_found_in_scope": 1}
    by_pointer = {item["pointer"]: item for item in operation_refs["items"]}
    assert by_pointer["/inputProfile"]["relationship"] == "operation.inputProfile"
    assert by_pointer["/parameter/0/part/0/targetProfile/0"]["status"] == "resolved"
    assert by_pointer["/parameter/0/part/0/binding/valueSet"]["status"] == "not_found_in_scope"

    capability_refs = search.inspect("Capability", config_path=config).data["references"]
    assert capability_refs["counts"] == {
        "resolved": 3,
        "not_found_in_scope": 1,
        "unsupported": 1,
    }
    assert {
        item["relationship"] for item in capability_refs["items"] if item["status"] == "resolved"
    } == {"capability.profile", "capability.supportedProfile", "capability.operation"}
    incoming = search.inspect("Input", view="incoming", limit=100, config_path=config).data[
        "incoming_references"
    ]
    assert incoming["counts"] == {
        "capability.profile": 1,
        "operation.inputProfile": 1,
        "operation.parameter.targetProfile": 1,
    }
    assert (
        search.inspect("BaseOperation", view="incoming", config_path=config).data[
            "incoming_references"
        ]["items"][0]["relationship"]
        == "operation.base"
    )
