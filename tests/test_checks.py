import json

import pytest
from typer.testing import CliRunner

from specfhir import checks, packages, search, validator
from specfhir.cli import app
from specfhir.models import DocumentPin, Lock, PackagePin, PublicationPin, Result


def test_readiness_and_provenance(tmp_path, monkeypatch):
    key = "example#1.0.0"
    pin = PackagePin(key=key, url="https://example.org/p.tgz", sha256="a" * 64, dependencies=[])
    publication = PublicationPin(
        package=key, url="https://example.org/full-ig.zip", sha256="a" * 64
    )
    page = DocumentPin(
        package=key,
        url="https://example.org/page.html",
        title="Page",
        sha256="b" * 64,
        publication=publication.url,
        member="site/page.html",
    )
    config = tmp_path / "custom.toml"
    (tmp_path / "specfhir.lock").write_text(
        Lock(
            roots=[key], packages=[pin], publications=[publication], documents=[page]
        ).model_dump_json()
    )
    monkeypatch.setattr(
        packages,
        "inventory",
        lambda _: {
            "index_matches_lock": True,
            "config_matches_lock": True,
            "validator": {"ready": False, "matches_lock": False},
        },
    )
    monkeypatch.setattr(
        packages, "pages", lambda *a: {"pages": [{"selected": True, "path": "page.html"}]}
    )
    monkeypatch.setattr(
        search,
        "inspect",
        lambda *a, **kw: Result(
            status="ok",
            data={
                "source": {"package": key, "artifact_version": "1.0.0"},
                "resource": {
                    "source_sha256": page.sha256,
                    "publication": publication.url,
                    "publication_member": page.member,
                },
            },
        ),
    )
    monkeypatch.setattr(
        validator, "validate", lambda *a, **kw: pytest.fail("Unexpected validation")
    )
    runner = CliRunner()
    command = ["check", "--config", str(config), "--json"]
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["counts"] == {"passed": 2, "skipped": 1}
    result = runner.invoke(app, [*command, "--with-validator"])
    assert result.exit_code == 1
    monkeypatch.setattr(search, "inspect", lambda *a, **kw: Result(status="not_found"))
    result = runner.invoke(app, command)
    assert result.exit_code == 1 and "provenance differs" in result.output
    result = runner.invoke(app, [*command, "--package", "example#2.0.0"])
    assert result.exit_code == 1 and "not locked" in result.output
    assert checks.run(config)["status"] == "error"
