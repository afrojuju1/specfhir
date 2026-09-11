import json

import httpx
import pytest
from helpers import archive
from typer.testing import CliRunner

from specfhir import index, packages, search
from specfhir.cli import app


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
    with pytest.raises(ValueError, match="Checksum mismatch"):
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
