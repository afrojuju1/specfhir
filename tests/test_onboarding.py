import json

import httpx
import pytest
from test_phase1 import archive
from typer.testing import CliRunner

from specfhir import index, packages, search, validator
from specfhir.cli import app
from specfhir.models import Result


def test_package_inventory_and_versioned_capabilities(tmp_path, database, monkeypatch):
    config = tmp_path / "specfhir.toml"
    roots = ["example.guide#1.0.0", "example.guide#2.0.0"]
    config.write_text(f'packages={json.dumps(roots)}\ndefault_package="{roots[0]}"\n')
    for key in roots:
        archive(
            tmp_path / ".specfhir/packages",
            key,
            [
                {
                    "resourceType": "CapabilityStatement",
                    "id": "payer",
                    "name": "Payer",
                    "url": "https://example.org/payer",
                    "version": key.split("#")[1],
                    "description": f"Payer authorization behavior in {key}",
                }
            ],
        )
    index.sync(config)
    for key in roots:
        result = search.inspect("Payer", package=key, view="raw", config_path=config)
        assert result.status == "ok"
        assert key in json.dumps(result.model_dump())
        result = search.search(
            "authorization",
            package=key,
            resource_type="CapabilityStatement",
            mode="lexical",
            config_path=config,
        )
        assert result.status == "ok"
        assert roots[1 - roots.index(key)] not in json.dumps(result.model_dump())
    monkeypatch.setattr(
        packages.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, request=httpx.Request("GET", a[0]), json={"ready": True, "snapshot_id": "stale"}
        ),
    )
    report = packages.inventory(config)
    assert report["index_matches_lock"] and report["config_matches_lock"]
    assert report["validator"]["matches_lock"] is False
    assert report["counts"]["artifacts"] == 2


def test_build_manifest_results(tmp_path, monkeypatch):
    path = tmp_path / "cases.json"
    (tmp_path / "instance.json").write_text('{"resourceType":"Patient"}')
    cases = [
        {
            "name": "first",
            "instance": "instance.json",
            "package": "example#1.0.0",
            "profile": "Patient",
        }
    ]
    path.write_text(json.dumps({"cases": cases}))
    calls = []

    def validate(instance, **kwargs):
        calls.append(kwargs)
        return Result(
            status="ok", data={"execution": "completed", "findings": {"errors": 2, "warnings": 1}}
        )

    monkeypatch.setattr(validator, "validate", validate)
    runner = CliRunner()
    result = runner.invoke(app, ["validate-cases", str(path), "--json"])
    assert result.exit_code == 4, result.stdout
    assert calls[0]["package"] == "example#1.0.0"
    cases.append({**cases[0], "name": "missing", "instance": "missing.json"})
    path.write_text(json.dumps({"cases": cases}))
    result = runner.invoke(app, ["validate-cases", str(path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["data"]["execution_failures"] == 1
    cases.append(cases[0])
    path.write_text(json.dumps({"cases": cases}))
    with pytest.raises(ValueError, match="unique"):
        validator.validate_cases(path, tmp_path / "specfhir.toml")


def test_refresh_failure_is_explicit(tmp_path, monkeypatch):
    import subprocess

    config = tmp_path / "specfhir.toml"
    config.write_text('packages=["example.guide#1.0.0"]\ndefault_package="example.guide#1.0.0"\n')
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    config.with_suffix(".lock").write_text('{"roots":["example.guide#1.0.0"],"packages":[]}')
    monkeypatch.setattr(validator, "setup", lambda path: {"snapshot_id": "expected"})
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["cwd"] == tmp_path

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        validator.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, request=httpx.Request("GET", a[0]), json={"ready": True, "snapshot_id": "stale"}
        ),
    )
    with pytest.raises(ValueError, match="Index sync completed but validator refresh failed"):
        validator.refresh(config)
    assert calls[0][-1] == "validator"
    from specfhir.config import load
    from specfhir.models import Lock

    expected = validator.snapshot_identity(
        Lock.model_validate_json(config.with_suffix(".lock").read_bytes()),
        load(config).default_package,
    )
    monkeypatch.setattr(
        validator.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            request=httpx.Request("GET", a[0]),
            json={"ready": True, "snapshot_id": expected, "mode": "offline"},
        ),
    )
    assert validator.refresh(config)["status"] == "unchanged"
    assert len(calls) == 1


def test_selected_core_and_pinned_documentation(tmp_path, database, monkeypatch):
    from contextlib import contextmanager

    from specfhir import db, documents
    from specfhir.models import Lock

    core = "hl7.fhir.r4.core#4.0.1"
    backport = "hl7.fhir.uv.subscriptions-backport.r4#1.1.0"
    config = tmp_path / "specfhir.toml"
    config.write_text(
        f'packages=["{core}","{backport}"]\ndefault_package="{backport}"\n'
        f'[[documents]]\npackage="{backport}"\nurl="https://example.org/STU1/spec.html"\n'
        'title="Pinned workflow"\n'
    )
    cache = tmp_path / ".specfhir/packages"
    archive(cache, core)
    archive(
        cache,
        backport,
        [
            {
                "resourceType": "ImplementationGuide",
                "id": "guide",
                "fhirVersion": ["4.0.1"],
                "name": "Guide",
            }
        ],
        deps={"hl7.fhir.r4.core": "4.0.0"},
    )
    html = (
        b'<div>navigation</div><div id="segment-content">'
        b"<p>Prior authorization workflow</p></div><div>footer</div>"
    )

    @contextmanager
    def fetch(*args, **kwargs):
        yield httpx.Response(200, content=html, request=httpx.Request("GET", args[1]))

    monkeypatch.setattr(httpx, "stream", fetch)
    report = index.sync(config)
    assert report["counts"]["publication_pages"] == 1
    assert search.inspect("Guide", view="raw", package=backport, config_path=config).status == "ok"
    lock = Lock.model_validate_json(config.with_suffix(".lock").read_bytes())
    pin = next(p for p in lock.packages if p.key == backport)
    assert pin.dependencies == ["hl7.fhir.r4.core#4.0.0"]
    assert packages.effective_dependencies(pin) == [core]
    assert index.sync(config)["status"] == "unchanged"
    with db.connect() as conn:
        edges = conn.execute("SELECT * FROM package_dependencies").fetchall()
        assert edges == [{"package_key": backport, "dependency_key": core}]
    found = search.search(
        "authorization",
        package=backport,
        resource_type="Documentation",
        mode="lexical",
        config_path=config,
    )
    assert found.status == "ok"
    assert "https://example.org/STU1/spec.html" in json.dumps(found.model_dump())
    assert documents.page_text(html.decode()) == "Prior authorization workflow"
    pin.dependency_resolutions = {"hl7.fhir.r4.core#4.0.0": "hl7.fhir.r4.core#4.0.2"}
    with pytest.raises(ValueError, match="Unapproved"):
        packages.effective_dependencies(pin)
    assert packages.core_resolutions("other#1.0.0", pin.dependencies) == {}
    page = next((tmp_path / ".specfhir/documents").glob("*.html"))
    page.write_text("changed")
    with pytest.raises(ValueError, match="checksum changed"):
        index.sync(config)


def test_secondary_registry_origin_survives_cache_reuse(tmp_path, monkeypatch):
    from specfhir.config import Config

    archive(tmp_path / "source", "example.guide#1.0.0")
    payload = (tmp_path / "source/example.guide#1.0.0.tgz").read_bytes()
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(
            404 if request.url.host == "packages.fhir.org" else 200, content=payload
        )

    def stream(*args, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond)).stream(*args, **kwargs)

    monkeypatch.setattr(httpx, "stream", stream)
    config = Config(packages=["example.guide#1.0.0"], default_package="example.guide#1.0.0")
    cache = tmp_path / "cache"
    first = packages.resolve_lock(config, cache, None)
    assert first.packages[0].url == "https://packages2.fhir.org/packages/example.guide/1.0.0"
    assert packages.resolve_lock(config, cache, None) == first
    assert len(calls) == 2
    (cache / "example.guide#1.0.0.tgz").unlink()
    assert packages.resolve_lock(config, cache, first) == first
    assert len(calls) == 3 and "packages2.fhir.org" in calls[-1]


def test_document_update_preserves_old_lock(tmp_path, monkeypatch):
    from specfhir import documents
    from specfhir.models import DocumentSource

    source = DocumentSource(
        package="example#1.0.0", url="https://example.org/STU1/page.html", title="Workflow"
    )
    content = b'<div id="segment-content">Original workflow</div>'

    def stream(*args, **kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
        ).stream(*args, **kwargs)

    monkeypatch.setattr(httpx, "stream", stream)
    old = documents.pin_pages([source], None, tmp_path)
    content = b'<div id="segment-content">Updated workflow</div>'
    new = documents.pin_pages([source], None, tmp_path)
    assert old[0].sha256 != new[0].sha256
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected download"))
    assert documents.pin_pages([source], old, tmp_path) == old
    assert documents.pin_pages([source], new, tmp_path) == new


def test_prepared_package_reuse_and_timings(tmp_path, database, monkeypatch):
    from specfhir import db

    config = tmp_path / "specfhir.toml"
    cache = tmp_path / ".specfhir/packages"
    config.write_text('packages=["example.b#1.0.0"]\ndefault_package="example.b#1.0.0"\n')
    resource = {
        "resourceType": "StructureDefinition",
        "id": "B",
        "name": "B",
        "snapshot": {"element": [{"id": "Patient", "path": "Patient", "min": 0}]},
    }
    archive(cache, "example.b#1.0.0", [resource])
    first = index.sync(config)
    assert first["preparation_cache"] == {"hits": 0, "misses": 1}
    unchanged = index.sync(config)
    assert unchanged["timings"]["extraction_seconds"] == 0
    assert "preparation_seconds" not in unchanged
    before = search.resolve("B", config_path=config)
    # Adding a preceding package shifts artifact IDs, but reused elements still join correctly.
    archive(cache, "example.a#1.0.0", [{"resourceType": "ValueSet", "id": "A"}])
    config.write_text(
        'packages=["example.a#1.0.0","example.b#1.0.0"]\ndefault_package="example.b#1.0.0"\n'
    )
    original = index.prepare_package
    calls = []

    def observed(lock, cache, spool):
        calls.extend(p.key for p in lock.packages)
        return original(lock, cache, spool)

    monkeypatch.setattr(index, "prepare_package", observed)
    rebuilt = index.sync(config, update_lock=True)
    assert calls == ["example.a#1.0.0"]
    assert rebuilt["preparation_cache"] == {"hits": 1, "misses": 1}
    assert search.resolve("B", config_path=config) == before
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT a.package_key FROM elements e JOIN artifacts a ON a.id=e.artifact_id"
            ).fetchone()["package_key"]
            == "example.b#1.0.0"
        )
        conn.execute("DELETE FROM index_state")
    # A corrupt derived spool is regenerated, never published.
    for path in (tmp_path / ".specfhir/prepared").glob("*/artifacts.elements"):
        if path.stat().st_size:
            path.write_text("corrupt")
    calls.clear()
    assert index.sync(config)["preparation_cache"] == {"hits": 1, "misses": 1}
    assert calls == ["example.b#1.0.0"]
    assert search.resolve("B", config_path=config) == before
    # Extractor changes invalidate both entries, even with unchanged archives.
    monkeypatch.setattr(index, "PREPARATION_VERSION", index.PREPARATION_VERSION + 1)
    with db.connect() as conn:
        conn.execute("DELETE FROM index_state")
    assert index.sync(config)["preparation_cache"] == {"hits": 0, "misses": 2}


def test_publication_discovery_pinning_and_offline_rebuild(tmp_path, database, monkeypatch):
    import hashlib
    import io
    import zipfile

    from specfhir import db, documents
    from specfhir.models import Lock

    config = tmp_path / "specfhir.toml"
    key = "example.guide#1.0.0"
    config.write_text(
        f'packages=["{key}"]\ndefault_package="{key}"\n'
        f'[[publications]]\npackage="{key}"\n'
        'url="https://example.org/1.0.0/full-ig.zip"\npage_prefix="en/"\n'
    )
    cache = tmp_path / ".specfhir/packages"
    guide = {
        "resourceType": "ImplementationGuide",
        "id": "guide",
        "packageId": "example.guide",
        "version": "1.0.0",
        "fhirVersion": ["4.0.1"],
        "definition": {
            "page": {
                "nameUrl": "toc.html",
                "title": "Contents",
                "page": [
                    {"nameUrl": "workflow.html", "title": "Workflow"},
                    {"nameUrl": "https://external.example/page.html", "title": "External"},
                    {"nameUrl": "ImplementationGuide-guide.html", "title": "Generated guide"},
                ],
            }
        },
    }
    # Use publication-standard filenames; the fixture helper uses numeric filenames.
    import tarfile

    cache.mkdir(parents=True)
    package_path = cache / f"{key}.tgz"
    with tarfile.open(package_path, "w:gz") as tar:
        for name, resource in {
            "package/package.json": {
                "name": "example.guide",
                "version": "1.0.0",
                "fhirVersions": ["4.0.1"],
            },
            "package/ImplementationGuide-guide.json": guide,
        }.items():
            raw = json.dumps(resource).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    page = (
        b'<div>Navigation</div><div id="segment-content">'
        b"<p>Authorization workflow requirements</p></div>"
    )

    def bundle(extra=None, package=None, include_page=True):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zip:
            zip.writestr(
                "site/package.tgz", package if package is not None else package_path.read_bytes()
            )
            if include_page:
                zip.writestr("site/en/workflow.html", page)
            if extra:
                zip.writestr(extra, "unsafe")
        return stream.getvalue()

    payload = bundle()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(args[1])
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        ).stream(*args, **kwargs)

    monkeypatch.setattr(httpx, "stream", fetch)
    first = index.sync(config)
    assert first["counts"]["publication_pages"] == 1
    assert calls == ["https://example.org/1.0.0/full-ig.zip"]
    lock = Lock.model_validate_json(config.with_suffix(".lock").read_bytes())
    assert lock.publications[0].sha256 == hashlib.sha256(payload).hexdigest()
    assert lock.documents[0].member == "site/en/workflow.html"
    assert lock.documents[0].url == "https://example.org/1.0.0/en/workflow.html"
    preview = packages.pages(key, config)
    assert preview["selected"] == 1
    assert all(p["excluded_reason"] for p in preview["pages"] if not p["selected"])
    cli = CliRunner().invoke(app, ["packages", "pages", key, "--config", str(config), "--json"])
    assert cli.exit_code == 0 and json.loads(cli.stdout) == preview
    before = search.search(
        "authorization",
        package=key,
        resource_type="Documentation",
        mode="lexical",
        config_path=config,
    )
    assert before.data and before.data["results"]
    assert "Navigation" not in json.dumps(before.model_dump())
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected download"))
    # Regenerate a missing page from the pinned ZIP without network access.
    for path in (tmp_path / ".specfhir/documents").glob("*.html"):
        path.unlink()
    assert index.sync(config)["status"] == "unchanged"
    with db.connect() as conn:
        conn.execute("DELETE FROM index_state")
    assert index.sync(config)["status"] == "synced"
    assert (
        search.search(
            "authorization",
            package=key,
            resource_type="Documentation",
            mode="lexical",
            config_path=config,
        )
        == before
    )
    zip_path = next((tmp_path / ".specfhir/publications").glob("*.zip"))
    zip_path.write_bytes(bundle(extra="../escape.html"))
    with pytest.raises(ValueError, match="checksum changed"):
        index.sync(config)
    for bad, error in [
        (bundle(extra="../escape.html"), "Unsafe publication archive"),
        (bundle(package=b"different release"), "embedded package differs"),
        (bundle(include_page=False), "Selected IG page missing"),
    ]:
        zip_path.write_bytes(bad)
        with pytest.raises(ValueError, match=error):
            index.sync(config, update_lock=True)
        assert (
            search.search(
                "authorization",
                package=key,
                resource_type="Documentation",
                mode="lexical",
                config_path=config,
            )
            == before
        )
    zip_path.write_bytes(payload)
    assert index.sync(config)["status"] == "unchanged"
    # Cache tampering is never hidden by copying the original bytes back over it.
    (tmp_path / ".specfhir/documents" / f"{lock.documents[0].sha256}.html").write_text("changed")
    with pytest.raises(ValueError, match="Cached documentation checksum changed"):
        index.sync(config)
    assert documents.page_candidates(package_path, key)[1]["selected"]
