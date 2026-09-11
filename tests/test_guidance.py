import json

import httpx
import pytest
from helpers import archive, profile
from typer.testing import CliRunner

from specfhir import checks, documents, index, search
from specfhir.cli import app
from specfhir.models import Error


def test_publication_sections():
    html = (
        '<div>outside</div><div id="segment-content"><p>Intro</p>'
        '<a name="slicing"></a><h3>Slicing</h3><div><p>Use slices.</p>'
        '<a href="other.html#rule">Rule</a><script>hidden</script></div>'
        '<h4><a id="child"></a>Child</h4><p>Details.</p></div><p>footer</p>'
    )
    sections = documents.page_sections(html)
    assert [s["anchor"] for s in sections] == [None, "slicing", "child"]
    assert sections[1]["links"] == ["other.html#rule"]
    assert "hidden" not in str(sections) and "footer" not in str(sections)
    assert documents.page_sections(html, ["slicing"]) == [sections[1]]
    resource = {
        "resourceType": "Documentation",
        "url": "https://example.org/v1/page.html",
        "source_sha256": "a" * 64,
        "sections": [{**sections[1], "links": ["other.html", "http://[", "javascript:bad"]}],
    }
    context = search.passage_context(resource, "/sections/0/text")
    assert context["links_unusable"] == 2 and context["links_total"] == 1
    with pytest.raises(ValueError, match="anchors"):
        documents.page_sections(html.replace('id="child"', 'id="slicing"'), ["slicing"])
    for anchors in (["absent"], ["slicing", "slicing"]):
        with pytest.raises(ValueError, match="anchors"):
            documents.page_sections(html, anchors)


def test_scoped_passage_continuation(tmp_path, database, monkeypatch):
    roots = ["example#1.0.0", "example#2.0.0"]
    config = tmp_path / "specfhir.toml"
    urls = ["https://example.org/v1/page.html", "https://example.org/v2/page.html"]
    config.write_text(
        f'packages={json.dumps(roots)}\ndefault_package="{roots[0]}"\n'
        + "".join(
            f'[[documents]]\npackage="{p}"\nurl="{url}"\ntitle="Guide"\nanchors=["rules"]\n'
            for p, url in zip(roots, urls, strict=True)
        )
    )
    for p in roots:
        resource = profile()
        resource["url"] = "https://example.org/definition#literal"
        resource["description"] = "Exact canonical fragment evidence"
        archive(tmp_path / ".specfhir/packages", p, [resource])
    html = (
        '<div id="segment-content"><h2 id="rules">Rules</h2><p>'
        + "cardinality rule " * 600
        + '</p><a href="other.html#rule">More</a>'
        + '<h3 id="next">Next section</h3>Other evidence</div>'
    ).encode()
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda *a, **k: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=html))
        ).stream(*a, **k),
    )
    index.sync(config)
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected download"))
    assert index.sync(config)["status"] == "unchanged"
    assert checks.run(config)["status"] == "ok"  # Direct pages use the same provenance checks.
    found = search.search(
        "cardinality",
        package=roots[0],
        resource_type="Documentation",
        mode="lexical",
        config_path=config,
    )
    hit = found.data["results"][0]
    assert hit["relationship"] == "relevance_candidate"
    assert hit["citation_url"] == urls[0] + "#rules"
    assert hit["links"] == [
        {"url": "https://example.org/v1/other.html#rule", "relationship": "published_link"}
    ]
    kwargs = dict(package=roots[0], view="passages", limit=1, config_path=config)
    exact = search.inspect("https://example.org/definition#literal", **kwargs)
    assert exact.status == "ok"
    assert exact.data["source"]["canonical"] == "https://example.org/definition#literal"
    first = search.inspect(urls[0] + "#rules", **kwargs)
    assert first.data["total"] > 1 and first.data["next_offset"] == 1
    parts = list(first.data["passages"])
    offset = first.data["next_offset"]
    while offset is not None:
        following = search.inspect(
            urls[0],
            pointer=hit["source"]["pointer"],
            offset=offset,
            dataset_id=first.dataset_id,
            **kwargs,
        )
        parts.extend(following.data["passages"])
        offset = following.data["next_offset"]
    raw = search.inspect(urls[0], package=roots[0], view="raw", config_path=config)
    assert "".join(p["text"] for p in parts).replace(" ", "").replace("\n", "") == raw.data[
        "resource"
    ]["sections"][0]["text"].replace(" ", "").replace("\n", "")
    cli = CliRunner().invoke(
        app,
        [
            "inspect",
            urls[0] + "#rules",
            "--package",
            roots[0],
            "--view",
            "passages",
            "--limit",
            "1",
            "--config",
            str(config),
            "--json",
        ],
    )
    assert cli.exit_code == 0 and json.loads(cli.stdout) == first.model_dump(exclude_none=True)
    for bad in (dict(offset=1), dict(dataset_id="stale"), dict(limit=101), dict(offset=-1)):
        with pytest.raises(Error):
            search.inspect(urls[0], **{**kwargs, **bad})
    with pytest.raises(Error):
        search.search("cardinality", dataset_id="stale", config_path=config)
    assert search.inspect(urls[1], **kwargs).status == "not_found"
    assert search.inspect(urls[0] + "#missing", **kwargs).status == "not_found"
    assert search.inspect(urls[0] + "#next", **kwargs).status == "not_found"
    assert search.inspect("https://example.org/v1/other.html#rule", **kwargs).status == "not_found"
    with pytest.raises(Error):
        search.inspect(urls[0] + "#rules", pointer="/sections/1/text", **kwargs)

    prior = first.dataset_id
    monkeypatch.setattr(index, "PAGE_PREPARATION_VERSION", index.PAGE_PREPARATION_VERSION + 1)
    assert checks.run(config)["status"] == "error"
    assert index.sync(config)["status"] == "synced"
    assert checks.run(config)["status"] == "ok"
    assert search.inspect(urls[0], **kwargs).dataset_id != prior
    with pytest.raises(Error):
        search.inspect(urls[0], dataset_id=prior, **kwargs)
