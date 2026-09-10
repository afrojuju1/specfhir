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


def test_validation_context_matrix(tmp_path, database, monkeypatch):
    from specfhir.config import digest
    from specfhir.models import Error

    packages = ["example#1.0.0", "example#2.0.0", "example#3.0.0"]
    contexts = [{"package": p, "profile": "PatientProfile"} for p in packages]
    config = tmp_path / "specfhir.toml"
    config.write_text(f'packages={json.dumps(packages)}\ndefault_package="{packages[0]}"\n')
    for p in packages:
        archive(tmp_path / ".specfhir/packages", p, [profile(version=p.split("#")[1])])
    index.sync(config)
    monkeypatch.setattr(validator, "support_lock", lambda: Lock(roots=[], packages=[]))
    calls = []
    scenario = "different"
    message_id = "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"

    def issue(location, **extra):
        return {
            "severity": "error",
            "code": "required",
            "expression": [location],
            "extension": [{"url": message_id, "valueCode": "REQUIRED"}],
            "diagnostics": "Same wording across different locations",
            "id": "original",
            **extra,
        }

    a = [issue("Patient.name"), issue("Patient.identifier")]
    b = [issue("Patient.name", severity="warning"), issue("Patient.gender")]

    @contextmanager
    def request(method, url, **kwargs):
        payload = kwargs["json"]
        calls.append(payload)
        middle = payload["package"] == packages[1]
        first = payload["package"] == packages[0]
        if (middle and scenario == "timeout") or (first and scenario == "first_timeout"):
            raise httpx.ReadTimeout("timeout")
        if middle and scenario == "busy":
            yield httpx.Response(429)
            return
        issues = b if middle and scenario == "different" else a
        if middle and scenario == "overflow":
            issues = [issue("Patient.name")] * 501
        reply = {
            "snapshot_id": payload["snapshot_id"],
            "package": payload["package"],
            "validator_sha256": validator.JAR_SHA256,
            "terminology_mode": "offline",
            "terminology_endpoint": "",
            "loaded_packages": [payload["package"]],
            "outcome": {"resourceType": "OperationOutcome", "issue": issues},
        }
        if middle and scenario == "substitution":
            reply["loaded_packages"] = [packages[0]]
        if first and scenario == "snapshot":
            config.write_text(
                config.read_text().replace(
                    f'default_package="{packages[0]}"', f'default_package="{packages[2]}"'
                )
            )
        if first and scenario == "publication":
            with db.connect() as conn:
                conn.execute("UPDATE index_state SET identity=%s", ("f" * 64,))
        yield httpx.Response(200, json=reply)

    monkeypatch.setattr(validator.httpx, "stream", request)
    instance = {
        "resourceType": "Patient",
        "id": "synthetic",
        "active": False,
        "extension": [{"url": "urn:example", "valueDecimal": 1.25}],
    }
    original = json.dumps(instance)
    args = dict(contexts=contexts, config_path=config)
    matrix = validator.validate(instance, **args)
    data = matrix.data
    assert matrix.status == "ok" and data["execution"] == "completed"
    assert not data["issues_identical"] and data["coverage"] == "per_context"
    assert data["instance_sha256"] == digest(instance) and json.dumps(instance) == original
    assert [c["package"] for c in calls] == packages  # N calls, never N*(N-1) pairs
    assert len({c["instance"] for c in calls}) == 1 and json.loads(calls[0]["instance"]) == instance
    assert [c["profile"].split("|")[1] for c in calls] == ["1.0.0", "2.0.0", "3.0.0"]
    assert [r["result"]["data"]["issues"] for r in data["results"]] == [a, b, a]
    correspondence = data["correspondence"]
    assert correspondence["counts"] == {"shared": 2, "context_only": 1}
    assert correspondence["available_contexts"] == [0, 1, 2]
    assert correspondence["unavailable_contexts"] == []
    for i in range(3):
        assert sorted(n for g in correspondence["items"] for n in g["issue_indices"][i]) == [0, 1]
    scenario = "same"
    for count in (1, 2, 3):
        before = len(calls)
        result = validator.validate(instance, contexts=contexts[:count], config_path=config)
        assert len(calls) == before + count and len(result.data["results"]) == count
        assert result.data["issues_identical"]
    for scenario in ("timeout", "busy", "substitution", "overflow", "first_timeout"):
        before = len(calls)
        result = validator.validate(instance, **args)
        data = result.data
        assert result.status == "error" and len(calls) == before + 3
        assert data["results"][2]["result"]["data"]["execution"] == "completed"
        missing = 0 if scenario == "first_timeout" else 1
        assert data["correspondence"]["status"] == "partial"
        assert data["correspondence"]["unavailable_contexts"][0]["context_index"] == missing
        assert all(g["issue_indices"][missing] is None for g in data["correspondence"]["items"])
        assert data["issues_identical"] is None
        if scenario == "overflow":
            assert (
                data["execution"] == "completed"
                and data["results"][1]["result"]["data"]["issue_count"] == 501
            )
        else:
            assert data["execution"] == "failed" and data["findings"] is None
            assert data["results"][missing]["result"]["data"]["coverage"] == "unknown"
    scenario = "same"
    selected = [contexts[0], {**contexts[1], "profile": "MissingProfile"}, contexts[2]]
    failed = validator.validate(instance, contexts=selected, config_path=config)
    assert failed.data["results"][1]["result"]["data"]["unresolved_profile"] == "MissingProfile"
    assert failed.data["correspondence"]["available_contexts"] == [0, 2]
    scenario = "different"
    path = tmp_path / "instance.json"
    path.write_text(original)
    for options in (
        ["--contexts", json.dumps(contexts)],
        [arg for p in packages for arg in ("--package", p)] + ["--profile", "PatientProfile"],
    ):
        cli = CliRunner().invoke(
            app, ["validate", str(path), *options, "--config", str(config), "--json"]
        )
        assert cli.exit_code == 4, cli.output
        assert json.loads(cli.stdout) == matrix.model_dump(exclude_none=True)
    before = len(calls)
    for value in ("null", "{}", "[]"):
        cli = CliRunner().invoke(
            app, ["validate", str(path), "--contexts", value, "--config", str(config), "--json"]
        )
        assert cli.exit_code == 1 and len(calls) == before
    assert validator.validate(instance, package="", config_path=config).status == "error"
    for bad in (
        [],
        contexts * 6,
        [contexts[0], contexts[0]],
        [{"package": "unpinned"}],
        [{"package": packages[0], "surprise": True}],
    ):
        with pytest.raises(ValueError):
            validator.validate(instance, contexts=bad, config_path=config)
    with pytest.raises(Error, match="not both"):
        validator.validate(instance, **args, package=packages[0])
    before = len(calls)
    for supplied in ("stale", ""):
        stale = validator.validate(instance, **args, dataset_id=supplied)
        assert (
            stale.status == "error"
            and stale.dataset_id == matrix.dataset_id
            and len(calls) == before
        )
        assert stale.data["correspondence"]["status"] == "unavailable"
    scenario = "snapshot"
    result = validator.validate(instance, **args)
    assert result.status == "error" and result.data["execution"] == "completed"
    assert result.data["correspondence"]["available_contexts"] == [0]
    assert len(result.data["correspondence"]["unavailable_contexts"]) == 2
    config.write_text(
        config.read_text().replace(
            f'default_package="{packages[2]}"', f'default_package="{packages[0]}"'
        )
    )
    scenario = "publication"
    before = len(calls)
    result = validator.validate(instance, **args)
    assert result.status == "error" and len(calls) == before + 1
    assert result.data["correspondence"]["available_contexts"] == [0]
    assert all(r["result"]["data"]["execution"] == "failed" for r in result.data["results"][1:])


def test_issue_correspondence_is_conservative():
    message_id = "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"
    located = {
        "severity": "error",
        "code": "required",
        "location": ["Patient.name[0]"],
        "extension": [{"url": message_id, "valueCode": "REQUIRED"}],
        "diagnostics": "Missing value",
    }
    unlocated = {"severity": "error", "code": "required", "diagnostics": "Missing value"}
    left = [located, located, unlocated, {**located, "location": ["Patient.name[1]"]}]
    right = [located, unlocated, {**located, "location": ["Patient.name[2]"]}]
    result = validator.issue_correspondence([left, right, None])
    assert result["counts"] == {"uncertain": 3, "context_only": 2}
    for i, issues in enumerate((left, right)):
        assert sorted(n for g in result["items"] for n in g["issue_indices"][i]) == list(
            range(len(issues))
        )
    assert all(g["issue_indices"][2] is None for g in result["items"])
    changed = validator.issue_correspondence(
        [[located], [{**located, "diagnostics": "Different constraint at same parent"}]]
    )
    assert changed["counts"] == {"uncertain": 1}
