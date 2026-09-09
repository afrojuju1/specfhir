"""Prepare off-transaction, publish atomically, preserve prior successful state."""

import hashlib
import json
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from specfhir import db, documents, embeddings, references
from specfhir.config import digest, load, lock_path
from specfhir.files import checksum
from specfhir.models import RESOURCE_TYPES, Error, Lock
from specfhir.packages import (
    archive_files,
    compatibility,
    effective_dependencies,
    manifest,
    resolve_lock,
    save_lock,
)


def prepare_package(lock: Lock, cache: Path, spool: Path) -> dict[str, Any]:
    inventory = []
    counts: Counter[str] = Counter()
    with (
        spool.open("w") as output,
        spool.with_suffix(".documents").open("w") as docs,
        spool.with_suffix(".elements").open("w") as elements,
        spool.with_suffix(".references").open("w") as links,
        spool.with_suffix(".excluded").open("w") as excluded,
    ):
        for pin in lock.packages:
            archive = cache / f"{pin.key}.tgz"
            info = manifest(archive, pin.key)
            reason = compatibility(info)
            skipped: Counter[str] = Counter()
            package_count = 0
            # Validate every archive member, even for packages excluded from indexing.
            for path, raw in archive_files(archive):
                if not path.startswith("package/") or not path.endswith(".json"):
                    continue
                if len(path.split("/")) != 2 or path.split("/")[-1].startswith("."):
                    skipped["nested_or_metadata"] += 1
                    continue
                if path == "package/package.json":
                    continue
                resource = json.loads(raw)
                if not isinstance(resource, dict):
                    raise Error(f"Expected a JSON object: {pin.key}/{path}")
                kind = resource.get("resourceType")
                if kind not in RESOURCE_TYPES:
                    skipped[str(kind or "non_resource")] += 1
                    continue
                if not isinstance(resource.get("id"), str):
                    raise Error(f"Missing resource id: {pin.key}/{path}")
                release = resource.get("fhirVersion", "4.0.1")
                compatible_release = (
                    isinstance(release, list) and "4.0.1" in release
                    if kind == "ImplementationGuide"
                    else release == "4.0.1"
                )
                if reason or not compatible_release:
                    if isinstance(resource.get("url"), str):
                        excluded.write(
                            json.dumps(
                                [
                                    pin.key,
                                    path,
                                    resource["url"],
                                    resource.get("version"),
                                    kind,
                                    reason or "Resource does not declare R4 4.0.1 support",
                                ]
                            )
                            + "\n"
                        )
                    skipped["incompatible_resource_release"] += 1
                    continue
                issues = {}
                for view in ("snapshot", "differential"):
                    representation = resource.get(view, {})
                    if not isinstance(representation, dict):
                        issues[view] = "Representation is not an object"
                        continue
                    entries = representation.get("element", [])
                    if not isinstance(entries, list):
                        issues[view] = "Element collection is not a list"
                        continue
                    seen = set()
                    for element in entries:
                        if not isinstance(element, dict):
                            issues[view] = "Element is not an object"
                            break
                        element_id = element.get("id")
                        if not isinstance(element_id, str) or not isinstance(
                            element.get("path"), str
                        ):
                            issues[view] = "Missing element id/path"
                            break
                        if element_id in seen:
                            issues[view] = f"Duplicate element id: {element_id}"
                            break
                        seen.add(element_id)
                    if view not in issues:
                        counts["elements"] += len(entries)
                if issues:
                    counts["artifacts_with_projection_issues"] += 1
                counts["artifacts"] += 1
                package_count += 1
                for link in references.extract(resource, issues):
                    links.write(json.dumps([counts["artifacts"], pin.key, *link]) + "\n")
                if kind == "StructureDefinition":
                    for view in ("snapshot", "differential"):
                        if view in issues:
                            continue
                        for ordinal, element in enumerate(
                            resource.get(view, {}).get("element", [])
                        ):
                            elements.write(
                                json.dumps(
                                    [
                                        counts["artifacts"],
                                        view,
                                        element["id"],
                                        element["path"],
                                        element.get("sliceName"),
                                        ordinal,
                                        element,
                                    ]
                                )
                                + "\n"
                            )
                for document in documents.extract(resource, issues):
                    docs.write(json.dumps([counts["artifacts"], *document]) + "\n")
                    counts["documents"] += 1
                output.write(
                    json.dumps([counts["artifacts"], pin.key, path, resource, issues]) + "\n"
                )
            inventory.append(
                {
                    "key": pin.key,
                    "manifest": info,
                    "sha256": pin.sha256,
                    "declared_dependencies": pin.dependencies,
                    "dependency_resolutions": pin.dependency_resolutions,
                    "excluded_reason": reason,
                    "artifacts": package_count,
                    "skipped": dict(skipped),
                }
            )
        for source in lock.documents:
            path = cache.parent / "documents" / f"{source.sha256}.html"
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != source.sha256:
                raise Error(f"Documentation checksum differs: {source.url}")
            resource = {
                "resourceType": "Documentation",
                "id": source.url.rsplit("/", 1)[-1],
                "url": source.url,
                "version": source.package.split("#")[1],
                "title": source.title,
                "description": documents.page_text(raw.decode("utf-8")),
                "source_sha256": source.sha256,
                "publication": source.publication,
                "publication_member": source.member,
            }
            counts["artifacts"] += 1
            counts["publication_pages"] += 1
            for document in documents.extract(resource, {}):
                docs.write(json.dumps([counts["artifacts"], *document]) + "\n")
                counts["documents"] += 1
            output.write(
                json.dumps([counts["artifacts"], source.package, source.url, resource, {}]) + "\n"
            )
            item = next(i for i in inventory if i["key"] == source.package)
            item["artifacts"] += 1
            item.setdefault("publication_pages", []).append(source.model_dump())
    return {"inventory": inventory, "counts": dict(counts)}


# Bump when extraction or spool semantics change (including documents.extract).
PREPARATION_VERSION = 2


def preparation_key(pin, pages):
    return digest(
        {
            "key": pin.key,
            "archive": pin.sha256,
            "documents": [d.model_dump() for d in pages],
            "version": PREPARATION_VERSION,
        }
    )


def prune_cache(lock: Lock, work: Path) -> dict:
    """Remove only recognized obsolete derived caches, while sync holds its lock."""
    import re

    keep = {
        preparation_key(p, [d for d in lock.documents if d.package == p.key]) for p in lock.packages
    }
    current_embedding = embeddings.cache_key(lock.embedding) if lock.embedding else None
    obsolete = []
    for path in () if (work / "prepared").is_symlink() else (work / "prepared").glob("*"):
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", path.name):
            continue
        if path.name not in keep:
            obsolete.append(path)
        else:
            obsolete.extend(
                p
                for p in path.glob("embedding-*")
                if p.is_dir()
                and not p.is_symlink()
                and re.fullmatch(r"embedding-[0-9a-f]{64}", p.name)
                and p.name != current_embedding
            )
    active_vectors = digest(lock.embedding) + ".sqlite" if lock.embedding else None
    obsolete.extend(
        p
        for p in (() if (work / "models").is_symlink() else (work / "models").glob("*.sqlite"))
        if p.is_file()
        and not p.is_symlink()
        and re.fullmatch(r"[0-9a-f]{64}\.sqlite", p.name)
        and p.name != active_vectors
    )
    reclaimed = 0
    for path in obsolete:
        reclaimed += (
            sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
            if path.is_dir()
            else path.stat().st_size
        )
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return {"removed_entries": len(obsolete), "reclaimed_bytes": reclaimed}


def prepare(lock: Lock, cache: Path, spool: Path) -> dict[str, Any]:
    """Reuse verified package projections, remapping local artifact IDs while streaming."""
    root = cache.parent / "prepared"
    root.mkdir(exist_ok=True)
    counts: Counter[str] = Counter()
    inventory = []
    hits = embedded_hits = embedded_misses = 0
    embedding_seconds = 0.0
    suffixes = (".jsonl", ".documents", ".elements", ".references", ".excluded")
    for suffix in suffixes:
        spool.with_suffix(suffix).write_text("")
    for pin in lock.packages:
        if checksum(cache / f"{pin.key}.tgz") != pin.sha256:
            raise Error(f"Checksum mismatch for {pin.key}")
        pages = [d for d in lock.documents if d.package == pin.key]
        for page in pages:
            if checksum(cache.parent / "documents" / f"{page.sha256}.html") != page.sha256:
                raise Error(f"Documentation checksum differs: {page.url}")
        identity = preparation_key(pin, pages)
        target = root / identity
        metadata = None
        try:
            candidate = json.loads((target / "metadata.json").read_text())
            if set(candidate["files"]) == {"artifacts" + s for s in suffixes} and all(
                checksum(target / name) == sha for name, sha in candidate["files"].items()
            ):
                metadata = candidate
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if metadata is None:
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                staging = Path(temporary) / "package"
                staging.mkdir()
                summary = prepare_package(
                    Lock(roots=[pin.key], packages=[pin], documents=pages),
                    cache,
                    staging / "artifacts.jsonl",
                )
                metadata = {
                    "summary": summary,
                    "files": {
                        "artifacts" + suffix: checksum(staging / ("artifacts" + suffix))
                        for suffix in suffixes
                    },
                }
                (staging / "metadata.json").write_text(json.dumps(metadata))
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
        else:
            hits += 1
        summary = metadata["summary"]
        document_path = target / "artifacts.documents"
        if lock.embedding:
            started = time.monotonic()
            document_path, embedded_counts, reused = embeddings.prepare_cached(
                cache.parent, target / "artifacts.jsonl", lock.embedding
            )
            embedding_seconds += time.monotonic() - started
            embedded_hits += reused
            embedded_misses += not reused
            summary = {**summary, "counts": {**summary["counts"], **embedded_counts}}
        offset = counts["artifacts"]
        for suffix in suffixes:
            with (
                (
                    document_path if suffix == ".documents" else target / ("artifacts" + suffix)
                ).open() as source,
                spool.with_suffix(suffix).open("a") as output,
            ):
                for line in source:
                    if suffix == ".excluded":
                        output.write(line)
                        continue
                    local_id, rest = line.split(",", 1)
                    output.write("[" + str(int(local_id[1:]) + offset) + "," + rest)
        counts.update(summary["counts"])
        item = dict(summary["inventory"][0])
        item.update(
            declared_dependencies=pin.dependencies,
            dependency_resolutions=pin.dependency_resolutions,
        )
        inventory.append(item)
    return {
        "inventory": inventory,
        "counts": dict(counts),
        "preparation_cache": {"hits": hits, "misses": len(lock.packages) - hits},
        "embedding_preparation_cache": {"hits": embedded_hits, "misses": embedded_misses},
        "embedding_seconds": embedding_seconds,
    }


def publish(conn, lock: Lock, spool: Path, identity: str, summary: dict[str, Any]):
    with conn.transaction():
        # DELETE (not TRUNCATE) lets readers retain the previous committed snapshot.
        for table in (
            "artifact_references",
            "excluded_artifacts",
            "documents",
            "elements",
            "artifacts",
            "package_dependencies",
            "packages",
            "index_state",
        ):
            conn.execute(f"DELETE FROM {table}")
        for item in summary["inventory"]:
            conn.execute(
                "INSERT INTO packages VALUES (%s,%s,%s,%s)",
                (
                    item["key"],
                    Jsonb(item["manifest"]),
                    item["sha256"],
                    item["excluded_reason"],
                ),
            )
        for pin in lock.packages:
            for dep in effective_dependencies(pin):
                conn.execute("INSERT INTO package_dependencies VALUES (%s,%s)", (pin.key, dep))
        with conn.cursor().copy("COPY artifacts FROM STDIN") as copy, spool.open() as stream:
            for line in stream:
                aid, key, path, resource, issues = json.loads(line)
                copy.write_row(
                    (
                        aid,
                        key,
                        path,
                        resource["resourceType"],
                        resource.get("id"),
                        resource.get("url"),
                        resource.get("version"),
                        resource.get("name"),
                        resource.get("title"),
                        Jsonb(resource),
                        Jsonb(issues),
                    )
                )
        with (
            conn.cursor().copy("COPY excluded_artifacts FROM STDIN") as copy,
            spool.with_suffix(".excluded").open() as stream,
        ):
            for line in stream:
                copy.write_row(json.loads(line))
        with (
            conn.cursor().copy("COPY elements FROM STDIN") as copy,
            spool.with_suffix(".elements").open() as stream,
        ):
            for line in stream:
                row = json.loads(line)
                row[-1] = Jsonb(row[-1])
                copy.write_row(row)
        with (
            conn.cursor().copy(
                "COPY documents (artifact_id,kind,pointer,element_id,representation,"
                "chunk,heading,text,text_hash,embedding) FROM STDIN"
            ) as copy,
            spool.with_suffix(".documents").open() as stream,
        ):
            for line in stream:
                row = json.loads(line)
                if len(row) == 9:
                    row.append(None)
                elif row[-1] is not None:
                    row[-1] = json.dumps(row[-1])
                copy.write_row(row)
        started = time.monotonic()
        summary["reference_checks"] = references.publish(conn, spool, summary["inventory"])
        reference_seconds = round(time.monotonic() - started, 3)
        conn.execute("INSERT INTO index_state VALUES (true,%s,%s)", (identity, Jsonb(summary)))
    return reference_seconds


def sync(
    config_path: Path = Path("specfhir.toml"),
    *,
    update_lock: bool = False,
    rebuild: bool = False,
    prune: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    timings = {}
    config_path = config_path.resolve()
    work = config_path.parent / ".specfhir"
    work.mkdir(exist_ok=True)
    project_lock = lock_path(config_path)
    with db.connect() as conn:
        # ponytail: one database-wide sync lock; scope by schema if concurrent indexes are needed.
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (db.SYNC_LOCK,)
        ).fetchone()
        if not acquired or not acquired["acquired"]:
            raise Error("Another sync is running; retry when it finishes")
        # Read config/lock only after acquiring the session lock to prevent stale writers.
        config = load(config_path)
        config_bytes = config_path.read_bytes()
        lock_bytes = project_lock.read_bytes() if project_lock.exists() else None
        existing = Lock.model_validate_json(lock_bytes) if lock_bytes else None
        previous = existing if not update_lock else None
        stage = time.monotonic()
        lock = resolve_lock(config, work / "packages", previous)
        lock.documents = documents.pin_pages(
            config.documents,
            [d for d in previous.documents if d.publication is None] if previous else None,
            work / "documents",
        )
        lock.publications, publication_pages = documents.pin_publications(
            config.publications, previous, lock.packages, work
        )
        lock.documents.extend(publication_pages)
        if len({d.url for d in lock.documents}) != len(lock.documents):
            raise Error("Duplicate direct/publication documentation URL")
        if config.embedding.enabled:
            lock.embedding = embeddings.pin_model(
                work, config.embedding, existing.embedding if existing else None, update_lock
            )
        timings["acquisition_seconds"] = round(time.monotonic() - stage, 3)
        identity = digest({"lock": lock.model_dump(), "schema": db.SCHEMA_VERSION})
        conn.execute(db.DDL)
        state = conn.execute("SELECT * FROM index_state").fetchone()
        if state and state["identity"] == identity and not rebuild:
            if previous is None:
                save_lock(project_lock, lock)
            metadata = dict(state["metadata"])
            metadata.pop("preparation_seconds", None)
            metadata["preparation_cache"] = {"hits": 0, "misses": 0}
            metadata["embedding_preparation_cache"] = {"hits": 0, "misses": 0}
            timings.update(
                extraction_seconds=0.0,
                embedding_seconds=0.0,
                publication_seconds=0.0,
                analyze_seconds=0.0,
                reference_seconds=0.0,
            )
            if prune:
                metadata["cache_cleanup"] = prune_cache(lock, work)
            timings["total_seconds"] = round(time.monotonic() - started, 3)
            return {"status": "unchanged", **metadata, "timings": timings}
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            spool = Path(temporary) / "artifacts.jsonl"
            stage = time.monotonic()
            summary = prepare(lock, work / "packages", spool)
            embedding_seconds = summary.pop("embedding_seconds")
            timings["extraction_seconds"] = round(time.monotonic() - stage - embedding_seconds, 3)
            timings["embedding_seconds"] = round(embedding_seconds, 3)
            summary["lock_digest"] = digest(lock.model_dump())
            summary["roots"] = lock.roots
            summary["schema_version"] = db.SCHEMA_VERSION
            summary["embedding"] = lock.embedding
            if (
                config_path.read_bytes() != config_bytes
                or (project_lock.read_bytes() if project_lock.exists() else None) != lock_bytes
            ):
                raise Error("Configuration or lock changed during sync; retry")
            # A failed publication may leave a pending lock, never mislabel old DB content.
            save_lock(project_lock, lock)
            stage = time.monotonic()
            timings["reference_seconds"] = publish(conn, lock, spool, identity, summary)
            timings["publication_seconds"] = round(time.monotonic() - stage, 3)
        stage = time.monotonic()
        conn.execute("ANALYZE")
        timings["analyze_seconds"] = round(time.monotonic() - stage, 3)
        if prune:
            summary["cache_cleanup"] = prune_cache(lock, work)
    timings["total_seconds"] = round(time.monotonic() - started, 3)
    return {"status": "synced", **summary, "timings": timings}
