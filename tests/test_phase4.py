import asyncio
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from helpers import archive, profile
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from specfhir import db, index, validator
from specfhir.cli import app
from specfhir.models import DocumentPin, Lock, PackagePin, PublicationPin


def test_validator_lifecycle(tmp_path, database, monkeypatch):
    config = tmp_path / "custom.toml"
    config.write_text(
        'packages=["example.patient#1.0.0"]\ndefault_package="example.patient#1.0.0"\n'
    )
    cache = tmp_path / ".specfhir/packages"
    archive(cache, "example.patient#1.0.0", [profile()])
    index.sync(config)
    support_reads = []

    def support():
        support_reads.append(True)
        return Lock(roots=[], packages=[])

    monkeypatch.setattr(validator, "support_lock", support)
    report = validator.setup(config)
    assert len(support_reads) == 1
    assert validator.setup(config)["snapshot_id"] == report["snapshot_id"]
    assert len(support_reads) == 2
    # Retrieval metadata cannot conflict with an existing immutable validator manifest.
    lock_path = config.with_name("specfhir.lock")
    original = lock_path.read_bytes()
    changed = Lock.model_validate_json(original)
    changed.embedding = {"revision": "different-retrieval-model"}
    changed.documents = [
        DocumentPin(
            package=changed.roots[0],
            url="https://example.org/page.html",
            title="New documentation",
            sha256="a" * 64,
        )
    ]
    manifest = tmp_path / ".specfhir/validator-service" / report["snapshot_id"] / "manifest.json"
    before_manifest = manifest.read_bytes()
    lock_path.write_text(changed.model_dump_json())
    assert validator.setup(config)["snapshot_id"] == report["snapshot_id"]
    assert len(support_reads) == 3
    assert manifest.read_bytes() == before_manifest
    lock_path.write_bytes(original)
    calls = []
    scenario = "valid"

    @contextmanager
    def request(url, **kwargs):
        calls.append(kwargs["json"])
        assert kwargs["trust_env"] is False
        payload = kwargs["json"]
        if scenario == "timeout":
            raise httpx.ReadTimeout("timeout")
        status = {"busy": 429, "stale": 409, "crash": 503}.get(scenario, 200)
        issues = [] if scenario == "valid" else [{"severity": "error", "code": "structure"}]
        reply = {
            "snapshot_id": payload["snapshot_id"],
            "package": payload["package"],
            "validator_sha256": validator.JAR_SHA256,
            "terminology_mode": "offline",
            "terminology_endpoint": "",
            "loaded_packages": ["example.patient#1.0.0"],
            "outcome": {"resourceType": "OperationOutcome", "issue": issues},
        }
        if scenario == "substitution":
            reply["loaded_packages"] = ["unexpected#1.0.0"]
        if scenario == "malformed":
            reply["outcome"] = {}
        yield httpx.Response(status, json=reply)

    monkeypatch.setattr(validator.httpx, "stream", lambda method, url, **kw: request(url, **kw))
    patient = {"resourceType": "Patient"}
    for scenario in (
        "valid",
        "invalid",
        "timeout",
        "busy",
        "stale",
        "crash",
        "substitution",
        "malformed",
    ):
        result = validator.validate(patient, profile="PatientProfile", config_path=config)
        assert result.data["execution"] == (
            "completed" if scenario in {"valid", "invalid"} else "failed"
        )
        if scenario in {"valid", "invalid"}:
            assert result.data["coverage"] == "limited"
            assert result.data["findings"]["errors"] == (scenario == "invalid")
    scenario = "invalid"
    instance = tmp_path / "patient.json"
    instance.write_text(json.dumps(patient))
    cli = CliRunner().invoke(app, ["validate", str(instance), "--config", str(config), "--json"])
    assert cli.exit_code == 4
    assert json.loads(cli.stdout)["data"]["execution"] == "completed"
    before = len(calls)
    for kwargs in (
        {"profile": "missing"},
        {"profile": "Patient.id"},
        {"package": "missing"},
        {"terminology_mode": "online"},
    ):
        assert validator.validate(patient, config_path=config, **kwargs).status == "error"
    declared = {**patient, "meta": {"profile": ["https://unknown.example/profile"]}}
    result = validator.validate(declared, config_path=config)
    assert result.data["unresolved_profile"] == "https://unknown.example/profile"
    assert len(calls) == before
    snapshot = tmp_path / ".specfhir/validator-service" / report["snapshot_id"]
    member = next((snapshot / ".fhir").rglob("PatientProfile.json"))
    member.write_text("{}")
    with pytest.raises(ValueError, match="corrupt"):
        validator.setup(config)
    (cache / "example.patient#1.0.0.tgz").unlink()
    with pytest.raises(ValueError, match="Missing or changed package"):
        validator.setup(config)


@pytest.mark.skipif(not os.getenv("SPECFHIR_VALIDATOR_SMOKE"), reason="Opt-in real HL7 smoke")
def test_real_validator_and_mcp(tmp_path, database):
    # Copy only the read-only lookup metadata into our disposable schema.
    # This smoke requires the application's R4 / US Core index in the same database.
    with db.connect() as conn:
        for table in ("index_state", "packages", "package_dependencies"):
            conn.execute(f"CREATE TABLE {table} AS SELECT * FROM public.{table}")
        conn.execute(
            "CREATE TABLE artifacts AS SELECT * FROM public.artifacts "
            "WHERE resource_id IN ('Patient','us-core-patient')"
        )
    repo = Path(__file__).resolve().parents[1]
    config = tmp_path / "custom.toml"
    shutil.copyfile(repo / "specfhir.toml", config)
    shutil.copyfile(repo / "specfhir.lock", config.with_name("specfhir.lock"))
    (tmp_path / ".specfhir").symlink_to(repo / ".specfhir", target_is_directory=True)
    valid = {
        "resourceType": "Patient",
        "id": "synthetic",
        "identifier": [
            {"system": "urn:uuid:7e559223-2d0b-4b38-aaba-a66ffbba3718", "value": "synthetic-123"}
        ],
        "name": [{"family": "Example", "given": ["Synthetic"]}],
        "gender": "unknown",
        "text": {
            "status": "generated",
            "div": '<div xmlns="http://www.w3.org/1999/xhtml">Synthetic test patient</div>',
        },
    }
    base = validator.validate(valid, package="hl7.fhir.r4.core#4.0.1", config_path=config)
    assert base.status == "ok", base.message
    assert base.data["findings"]["errors"] == 0
    good = validator.validate(valid, profile="USCorePatient", config_path=config)
    assert good.status == "ok", good.message
    assert good.data["findings"]["errors"] == 0
    invalid = {
        "resourceType": "Patient",
        "id": "synthetic",
        "managingOrganization": {"reference": "#missing"},
    }
    bad = validator.validate(invalid, profile="USCorePatient", config_path=config)
    assert bad.status == "ok", bad.message
    assert bad.data["findings"]["errors"] >= 2
    assert bad.data["coverage"] == "limited"
    assert bad.data["references"] == ["#missing"]
    assert any("missing" in json.dumps(issue) for issue in bad.data["issues"])
    assert "hl7.fhir.us.core#9.0.0" in bad.data["loaded_packages"]

    async def parity():
        params = StdioServerParameters(
            command=shutil.which("uv"),
            args=["run", "--no-sync", "specfhir", "mcp", "--config", str(config)],
            cwd=repo,
            env={
                "SPECFHIR_DSN": os.environ["SPECFHIR_DSN"],
            },
        )
        async with Client(params, read_timeout_seconds=180) as client:
            actual = await client.call_tool(
                "validate", {"instance": invalid, "profile": "USCorePatient"}
            )
            assert actual.structured_content == bad.model_dump(exclude_none=True)

    asyncio.run(parity())


def test_validator_identity_inputs(monkeypatch):
    pin = PackagePin(
        key="example#1.0.0", url="https://example.org/package.tgz", sha256="a" * 64, dependencies=[]
    )
    locked = Lock(roots=[pin.key], packages=[pin])
    support = Lock(roots=[], packages=[])
    identity = validator.snapshot_identity(locked, pin.key, support)
    changed = locked.model_copy(deep=True)
    changed.roots = []
    changed.embedding = {"model": "different"}
    changed.publications = [
        PublicationPin(package=pin.key, url="https://example.org/full-ig.zip", sha256="b" * 64)
    ]
    changed.documents = [
        DocumentPin(
            package=pin.key, url="https://example.org/page.html", title="Changed", sha256="c" * 64
        )
    ]
    changed.packages[0].url = "https://mirror.example.org/package.tgz"
    assert validator.snapshot_identity(changed, pin.key, support) == identity
    for field, value in (
        ("key", "example#2.0.0"),
        ("sha256", "b" * 64),
        ("dependencies", ["dependency#1.0.0"]),
        ("dependency_resolutions", {"dependency#1.0.0": "dependency#1.0.1"}),
    ):
        changed = locked.model_copy(deep=True)
        setattr(changed.packages[0], field, value)
        assert validator.snapshot_identity(changed, pin.key, support) != identity
    assert validator.snapshot_identity(locked, "another#1.0.0", support) != identity
    support.packages.append(pin)
    assert validator.snapshot_identity(locked, pin.key, support) != identity
    support.packages.clear()
    monkeypatch.setattr(validator, "JAR_SHA256", "f" * 64)
    assert validator.snapshot_identity(locked, pin.key, support) != identity
