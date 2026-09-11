import pytest
from helpers import archive, profile, project_config

from specfhir import index, search


def test_prepared_package_reuse_and_timings(tmp_path, database, monkeypatch):
    from specfhir import db

    config = project_config(tmp_path, ["example.b#1.0.0"])
    cache = tmp_path / ".specfhir/packages"
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
    after = search.resolve("B", config_path=config)
    assert after.dataset_id != before.dataset_id
    assert after.model_copy(update={"dataset_id": before.dataset_id}) == before
    before = after
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


def test_explicit_cache_cleanup_preserves_current_and_unknown_data(tmp_path, database):
    config = project_config(tmp_path, ["example#1.0.0"])
    work = tmp_path / ".specfhir"
    archive(work / "packages", "example#1.0.0", [profile()])
    first = index.sync(config)
    current = next((work / "prepared").iterdir())
    obsolete = work / "prepared" / ("0" * 64)
    obsolete.mkdir()
    (obsolete / "old").write_bytes(b"old")
    unknown = work / "prepared" / "notes.txt"
    unknown.write_text("retain")
    outside = tmp_path / "shared"
    outside.mkdir()
    (outside / "keep").write_text("retain")
    (work / "prepared" / ("1" * 64)).symlink_to(outside, target_is_directory=True)
    stale_embedding = current / ("embedding-" + "0" * 64)
    stale_embedding.mkdir()
    (stale_embedding / "old").write_bytes(b"old")
    (work / "models").mkdir()
    old_vectors = work / "models" / ("0" * 64 + ".sqlite")
    old_vectors.write_bytes(b"old")
    source = work / "packages/example#1.0.0.tgz"
    original = source.read_bytes()
    source.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        index.sync(config, prune=True)
    assert obsolete.exists()
    source.write_bytes(original)
    result = index.sync(config, prune=True)
    assert result["status"] == "unchanged"
    assert result["cache_cleanup"] == {"removed_entries": 3, "reclaimed_bytes": 9}
    assert current.is_dir() and source.read_bytes() == original
    assert unknown.read_text() == "retain" and (outside / "keep").read_text() == "retain"
    assert not obsolete.exists() and not stale_embedding.exists() and not old_vectors.exists()
    rebuilt = index.sync(config, rebuild=True)
    assert rebuilt["status"] == "synced" and rebuilt["counts"] == first["counts"]
