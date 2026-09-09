"""Reference findings use source-package scope and publish with the dataset."""

import json

import pytest
from test_phase1 import archive, profile
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
        [child, own, valueset],
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
    cli = CliRunner().invoke(app, ["inspect", "Child", "--config", str(config), "--json"])
    assert cli.exit_code == 0
    assert json.loads(cli.stdout)["data"]["references"] == result.data["references"]
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
