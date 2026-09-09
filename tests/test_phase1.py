"""Behavioral acceptance checks; PostgreSQL runs in a disposable test schema."""

import io
import json
import os
import tarfile
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from specfhir import db, index, packages, search
from specfhir.cli import app
from specfhir.config import Config
from specfhir.models import Error, Lock


def archive(cache, key, resources=(), deps=None, release="4.0.1"):
    cache.mkdir(parents=True, exist_ok=True)
    name, version = key.split("#")
    values = {
        "package/package.json": {
            "name": name,
            "version": version,
            "fhirVersions": [release],
            "dependencies": deps or {},
        }
    }
    values.update({f"package/{r['id']}.json": r for r in resources})
    with tarfile.open(cache / f"{key}.tgz", "w:gz") as output:
        for path, value in values.items():
            content = json.dumps(value).encode()
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))


def profile(name="PatientProfile", **extra):
    return {
        "resourceType": "StructureDefinition",
        "id": name,
        "name": name,
        "url": f"https://example.org/{name}",
        "version": "1.0.0",
        "type": "Patient",
        "snapshot": {
            "element": [
                {"id": "Patient", "path": "Patient", "min": 0, "max": "*"},
                {"id": "Patient.id", "path": "Patient.id", "min": 0, "max": "1"},
                {"id": "Patient.identifier", "path": "Patient.identifier", "min": 1, "max": "*"},
                {
                    "id": "Patient.identifier:mrn",
                    "path": "Patient.identifier",
                    "sliceName": "mrn",
                    "min": 1,
                    "max": "1",
                },
            ]
        },
        "differential": {"element": [{"id": "Patient", "path": "Patient"}]},
        **extra,
    }


@pytest.fixture
def project(tmp_path):
    cache = tmp_path / ".specfhir" / "packages"
    archive(cache, "example.dep#1.0.0", [profile("Duplicate")])
    archive(cache, "example.dep#2.0.0", [profile("Duplicate")])
    archive(cache, "example.r5#1.0.0", [profile("R5Only")], release="5.0.0")
    archive(cache, "example.branch#1.0.0", deps={"example.dep": "2.0.0"})
    broken = profile(
        "Broken",
        snapshot={
            "element": [
                {"id": "Patient", "path": "Patient"},
                {"id": "Patient", "path": "Patient"},
            ]
        },
    )
    archive(
        cache,
        "example.root#1.0.0",
        [profile(), profile("Differential", snapshot={}), broken],
        deps={"example.dep": "1.0.0", "example.branch": "1.0.0", "example.r5": "1.0.0"},
    )
    config = tmp_path / "specfhir.toml"
    config.write_text('packages = ["example.root#1.0.0"]\ndefault_package = "example.root#1.0.0"\n')
    return config


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("SPECFHIR_TEST_DSN")
    if not dsn:
        pytest.skip("Set SPECFHIR_TEST_DSN to run PostgreSQL acceptance tests")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setenv("SPECFHIR_DSN", make_conninfo(dsn, options=f"-c search_path={schema}"))
        try:
            yield
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_exact_graph_lock_and_archive_boundaries(project, tmp_path):
    config = Config(packages=["example.root#1.0.0"], default_package="example.root#1.0.0")
    cache = project.parent / ".specfhir" / "packages"
    lock = packages.resolve_lock(config, cache, None)
    assert len(lock.packages) == 5
    assert packages.resolve_lock(config, cache, lock) == lock
    archive(cache, "example.root#1.0.0", deps={"example.dep": "2.0.0"})
    with pytest.raises(Error, match="Checksum mismatch"):
        packages.resolve_lock(config, cache, lock)
    archive(cache, "example.root#1.0.0", deps={"example.root": "1.0.0"})
    with pytest.raises(Error, match="cycle"):
        packages.resolve_lock(config, cache, None)
    archive(cache, "example.root#1.0.0", deps={"example.dep": "2.x"})
    with pytest.raises(Error, match="ranges are unsupported"):
        packages.resolve_lock(config, cache, None)
    unsafe = tmp_path / "unsafe.tgz"
    with tarfile.open(unsafe, "w:gz") as output:
        output.addfile(tarfile.TarInfo("../outside"), io.BytesIO())
    with pytest.raises(Error, match="Unsafe archive path"):
        list(packages.archive_files(unsafe))
    with tarfile.open(unsafe, "w:gz") as output:
        link = tarfile.TarInfo("package/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        output.addfile(link)
    with pytest.raises(Error, match="Unsupported archive member"):
        list(packages.archive_files(unsafe))


def test_sync_lookup_failure_rebuild_and_removal(project, database, monkeypatch):
    # All inputs cached: this also proves offline sync/lookup do not require HTTP.
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected network call"))
    first = index.sync(project)
    assert first["counts"]["artifacts"] == 5
    assert first["counts"]["artifacts_with_projection_issues"] == 1
    assert any(item["excluded_reason"] for item in first["inventory"])
    assert index.sync(project)["status"] == "unchanged"
    result = search.resolve("Patient.id", config_path=project)
    assert result.status == "ok" and result.data["element"]["id"] == "Patient.id"
    assert result.data["source"]["pointer"] == "/snapshot/element/1"
    assert search.resolve("Patient.identifier", config_path=project).status == "ambiguous"
    assert search.resolve("Patient.identifier:mrn", config_path=project).status == "ok"
    assert search.resolve("Duplicate", config_path=project).status == "ambiguous"
    assert search.resolve("Duplicate", package="example.dep#2.0.0").status == "ok"
    assert (
        search.resolve(
            "https://example.org/PatientProfile|1.0.0", element="id", config_path=project
        )
        == result
    )
    for name in ("Differential", "Broken"):
        assert (
            search.resolve(name, config_path=project).status == "effective_definition_unavailable"
        )
        assert search.inspect(name, view="raw", config_path=project).status == "ok"
    assert search.inspect("Differential", view="differential", config_path=project).status == "ok"
    assert search.resolve("R5Only", config_path=project).status == "not_found"
    cli = CliRunner().invoke(app, ["resolve", "Patient.id", "--config", str(project), "--json"])
    assert cli.exit_code == 0 and json.loads(cli.stdout) == result.model_dump(exclude_none=True)

    # Force a DB failure after DELETE and package inserts, proving rollback protects old data.
    lock = Lock.model_validate_json(project.with_name("specfhir.lock").read_bytes())
    spool = project.parent / "invalid.jsonl"
    spool.write_text('[1,"missing.package#1.0.0","bad.json",{"resourceType":"ValueSet"},{}]\n')
    with db.connect() as conn:
        with pytest.raises(psycopg.IntegrityError):
            index.publish(conn, lock, spool, "invalid", first)
    assert search.resolve("Patient.id", config_path=project) == result

    # Serialized writers fail promptly rather than publishing stale work.
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (db.SYNC_LOCK,))
        with pytest.raises(Error, match="Another sync"):
            index.sync(project)

    # Rebuild from the same archives/lock reproduces exact structured results.
    with db.connect() as conn:
        conn.execute("DELETE FROM index_state")
    assert index.sync(project)["status"] == "synced"
    assert search.resolve("Patient.id", config_path=project) == result

    project.write_text('packages = ["example.dep#2.0.0"]\ndefault_package = "example.dep#2.0.0"\n')
    with pytest.raises(Error, match="Lock/config mismatch"):
        index.sync(project)
    assert search.resolve("Patient.id", package="example.root#1.0.0").status == "ok"
    assert index.sync(project, update_lock=True)["counts"]["artifacts"] == 1
    assert search.resolve("Patient.id", package="example.root#1.0.0").status == "not_found"


def test_download_checksum_and_cleanup(tmp_path, monkeypatch):
    payload = b"package archive bytes"

    def stream(*args, **kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        ).stream(*args, **kwargs)

    monkeypatch.setattr(httpx, "stream", stream)
    with pytest.raises(Error, match="Checksum mismatch"):
        packages.obtain(tmp_path, "example.root#1.0.0", "https://example.org/package", "0" * 64)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not os.environ.get("SPECFHIR_REAL_SMOKE"), reason="Set SPECFHIR_REAL_SMOKE=1")
def test_real_r4_us_core_rebuild(tmp_path, database, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    config = tmp_path / "specfhir.toml"
    config.write_bytes((repo / "specfhir.toml").read_bytes())
    (tmp_path / "specfhir.lock").write_bytes((repo / "specfhir.lock").read_bytes())
    (tmp_path / ".specfhir").mkdir()
    (tmp_path / ".specfhir/packages").symlink_to(
        repo / ".specfhir/packages", target_is_directory=True
    )
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected network call"))
    report = index.sync(config)
    result = search.resolve("USCorePatient.identifier", config_path=config)
    assert result.status == "ok"
    assert result.data["element"]["min"] == 1
    assert result.data["element"]["max"] == "*"
    assert result.data["element"]["mustSupport"] is True
    assert result.data["source"]["package"] == "hl7.fhir.us.core#9.0.0"
    assert search.resolve("USCorePatient.id", config_path=config).status == "ok"
    assert search.resolve("USCorePatient.extension", config_path=config).status == "ambiguous"
    assert search.resolve("USCorePatient.extension:race", config_path=config).status == "ok"
    assert index.sync(config)["status"] == "unchanged"
    with db.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"]
            == report["counts"]["artifacts"]
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM elements").fetchone()["n"]
            == report["counts"]["elements"]
        )
        conn.execute("DELETE FROM index_state")
    assert index.sync(config)["counts"] == report["counts"]
    assert search.resolve("USCorePatient.identifier", config_path=config) == result
