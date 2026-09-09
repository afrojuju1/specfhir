import json
from pathlib import Path

import httpx
import pytest
from helpers import archive, profile

from specfhir import db, embeddings, index, search
from specfhir.files import checksum
from specfhir.models import Error, Lock


def test_fusion_and_vector_validation():
    def row(name):
        return dict(
            canonical=name,
            version="1",
            element_id=None,
            text_hash=name,
            package_key="p",
            file_path=name,
            pointer="",
            chunk=0,
            score=10,
        )

    fused = search.fuse([row("a"), row("b")], [row("b"), row("c")])
    assert [r["canonical"] for r in fused] == ["b", "a", "c"]
    assert fused[0]["score"] == 1 / 62 + 1 / 61
    for values in ([0] * 384, [1] * 383, [float("nan")] * 384):
        with pytest.raises(Error, match="Invalid embedding"):
            embeddings.vector(values)


def test_real_model_atomicity_and_modes(tmp_path, database, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    model_dir = repo / ".specfhir/models" / embeddings.REVISION
    if not model_dir.exists():
        pytest.skip("Run embedding-enabled sync to download the pinned model")
    cache = tmp_path / ".specfhir/packages"
    resource = profile(
        description="This person requires an interpreter to communicate with healthcare staff."
    )
    resource["snapshot"]["element"][2]["definition"] = "An identifier assigned to the patient."
    archive(cache, "example.patient#1.0.0", [resource])
    archive(
        cache,
        "example.other#1.0.0",
        [profile("Other", description="This person requires an interpreter.")],
    )
    (tmp_path / ".specfhir/models").symlink_to(repo / ".specfhir/models", target_is_directory=True)
    config = tmp_path / "specfhir.toml"
    base = (
        'packages=["example.patient#1.0.0","example.other#1.0.0"]\n'
        'default_package="example.patient#1.0.0"\n'
    )
    config.write_text(base)
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    monkeypatch.setattr(
        embeddings, "snapshot_download", lambda *a, **k: pytest.fail("Unexpected model download")
    )
    index.sync(config)
    exact = search.resolve("Patient.id", config_path=config)
    with pytest.raises(Error, match="Semantic index unavailable"):
        search.search("translator", mode="hybrid", config_path=config)
    config.write_text(base + "[embedding]\nenabled=true\nmax_tokens=256\n")
    report = index.sync(config)
    assert report["counts"]["embeddings"] > 0
    assert search.resolve("Patient.id", config_path=config) == exact
    assert index.sync(config)["status"] == "unchanged"
    locked = Lock.model_validate_json(config.with_suffix(".lock").read_bytes())
    spool = tmp_path / "prepared.jsonl"
    original_prepare = embeddings.prepare
    with monkeypatch.context() as patch:
        patch.setattr(embeddings, "prepare", lambda *a: pytest.fail("Repeated preparation"))
        reused = index.prepare(locked, cache, spool)
    assert reused["embedding_preparation_cache"] == {"hits": 2, "misses": 0}
    expected = checksum(spool.with_suffix(".documents"))
    cached = next((cache.parent / "prepared").glob("*/embedding-*/artifacts.documents"))
    cached.write_text("corrupt")
    repaired = index.prepare(locked, cache, spool)
    assert repaired["embedding_preparation_cache"] == {"hits": 1, "misses": 1}
    assert checksum(spool.with_suffix(".documents")) == expected
    with monkeypatch.context() as patch:
        patch.setattr(embeddings, "PREPARATION_VERSION", embeddings.PREPARATION_VERSION + 1)
        calls = []

        def record(*args):
            calls.append(args)
            return original_prepare(*args)

        patch.setattr(embeddings, "prepare", record)
        changed = index.prepare(locked, cache, spool)
        assert changed["embedding_preparation_cache"] == {"hits": 0, "misses": 2}
        assert len(calls) == 2 and checksum(spool.with_suffix(".documents")) == expected
    for mode in ("semantic", "hybrid"):
        result = search.search(
            "Help someone who cannot speak the local language", mode=mode, config_path=config
        )
        assert result.data["mode"] == mode
        assert all(
            r["source"]["package"] == "example.patient#1.0.0" for r in result.data["results"]
        )
    pin = report["embedding"]
    _, tok = embeddings.load_model(str(tmp_path / ".specfhir"), json.dumps(pin, sort_keys=True))
    parts = list(embeddings.bounded_passages("medical identifier " * 1000, tok, 128))
    assert len(parts) > 1 and all(len(tok.encode(p).ids) <= 128 for p in parts)
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT min(public.vector_dims(embedding)) AS n FROM documents"
            ).fetchone()["n"]
            == 384
        )
    config.write_text(base + "[embedding]\nenabled=true\nmax_tokens=128\n")
    with pytest.raises(Error, match="differ from lock"):
        index.sync(config)
    original = embeddings.prepare
    monkeypatch.setattr(
        embeddings, "prepare", lambda *a, **k: (_ for _ in ()).throw(Error("Model failed"))
    )
    with pytest.raises(Error, match="Model failed"):
        index.sync(config, update_lock=True)
    assert search.resolve("Patient.id", config_path=config) == exact
    assert search.search("translator", mode="hybrid", config_path=config).status == "ok"
    monkeypatch.setattr(embeddings, "prepare", original)
    updated = index.sync(config, update_lock=True)
    assert updated["status"] == "synced" and updated["embedding"]["max_tokens"] == 128
    monkeypatch.setattr(
        embeddings,
        "query_vector",
        lambda *a, **k: (_ for _ in ()).throw(Error("Model unavailable")),
    )
    assert search.search("interpreter", mode="lexical", config_path=config).status == "ok"
    with pytest.raises(Error, match="Model unavailable"):
        search.search("interpreter", mode="auto", config_path=config)
