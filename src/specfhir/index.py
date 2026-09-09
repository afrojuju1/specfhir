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

from specfhir import db, documents, embeddings
from specfhir.config import digest, load
from specfhir.models import Error, Lock
from specfhir.packages import (
    archive_files,
    compatibility,
    effective_dependencies,
    manifest,
    resolve_lock,
    save_lock,
)

SUPPORTED = {
    "StructureDefinition",
    "SearchParameter",
    "ValueSet",
    "CodeSystem",
    "ConceptMap",
    "OperationDefinition",
    "ImplementationGuide",
    "CapabilityStatement",
}


def prepare_package(lock: Lock, cache: Path, spool: Path) -> dict[str, Any]:
    inventory = []
    counts: Counter[str] = Counter()
    with (
        spool.open("w") as output,
        spool.with_suffix(".documents").open("w") as docs,
        spool.with_suffix(".elements").open("w") as elements,
    ):
        for pin in lock.packages:
            archive = cache / f"{pin.key}.tgz"
            info = manifest(archive, pin.key)
            reason = compatibility(info)
            skipped: Counter[str] = Counter()
            package_count = 0
            # Validate every archive member, even for packages excluded from indexing.
            for path, raw in archive_files(archive):
                if reason or not path.startswith("package/") or not path.endswith(".json"):
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
                if kind not in SUPPORTED:
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
                if not compatible_release:
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
PREPARATION_VERSION = 1


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(lock: Lock, cache: Path, spool: Path) -> dict[str, Any]:
    """Reuse verified package projections, remapping local artifact IDs while streaming."""
    root = cache.parent / "prepared"
    root.mkdir(exist_ok=True)
    counts: Counter[str] = Counter()
    inventory = []
    hits = 0
    suffixes = (".jsonl", ".documents", ".elements")
    for suffix in suffixes:
        spool.with_suffix(suffix).write_text("")
    for pin in lock.packages:
        if file_digest(cache / f"{pin.key}.tgz") != pin.sha256:
            raise Error(f"Checksum mismatch for {pin.key}")
        pages = [d for d in lock.documents if d.package == pin.key]
        for page in pages:
            if file_digest(cache.parent / "documents" / f"{page.sha256}.html") != page.sha256:
                raise Error(f"Documentation checksum differs: {page.url}")
        identity = digest(
            {
                "key": pin.key,
                "archive": pin.sha256,
                "documents": [d.model_dump() for d in pages],
                "version": PREPARATION_VERSION,
            }
        )
        target = root / identity
        metadata = None
        try:
            candidate = json.loads((target / "metadata.json").read_text())
            if set(candidate["files"]) == {"artifacts" + s for s in suffixes} and all(
                file_digest(target / name) == sha for name, sha in candidate["files"].items()
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
                        "artifacts" + suffix: file_digest(staging / ("artifacts" + suffix))
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
        offset = counts["artifacts"]
        for suffix in suffixes:
            with (
                (target / ("artifacts" + suffix)).open() as source,
                spool.with_suffix(suffix).open("a") as output,
            ):
                for line in source:
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
    }


def publish(conn, lock: Lock, spool: Path, identity: str, summary: dict[str, Any]):
    with conn.transaction():
        # DELETE (not TRUNCATE) lets readers retain the previous committed snapshot.
        for table in (
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
        conn.execute("INSERT INTO index_state VALUES (true,%s,%s)", (identity, Jsonb(summary)))


def sync(config_path: Path = Path("specfhir.toml"), *, update_lock: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    timings = {}
    config_path = config_path.resolve()
    work = config_path.parent / ".specfhir"
    work.mkdir(exist_ok=True)
    lock_path = config_path.with_name("specfhir.lock")
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
        lock_bytes = lock_path.read_bytes() if lock_path.exists() else None
        existing = Lock.model_validate_json(lock_bytes) if lock_bytes else None
        previous = existing if not update_lock else None
        stage = time.monotonic()
        lock = resolve_lock(config, work / "packages", previous)
        lock.documents = documents.pin_pages(
            config.documents, previous.documents if previous else None, work / "documents"
        )
        if config.embedding.enabled:
            lock.embedding = embeddings.pin_model(
                work, config.embedding, existing.embedding if existing else None, update_lock
            )
        timings["acquisition_seconds"] = round(time.monotonic() - stage, 3)
        identity = digest({"lock": lock.model_dump(), "schema": db.SCHEMA_VERSION})
        conn.execute(db.DDL)
        state = conn.execute("SELECT * FROM index_state").fetchone()
        if state and state["identity"] == identity:
            if previous is None:
                save_lock(lock_path, lock)
            metadata = dict(state["metadata"])
            metadata.pop("preparation_seconds", None)
            metadata["preparation_cache"] = {"hits": 0, "misses": 0}
            timings.update(
                extraction_seconds=0.0,
                embedding_seconds=0.0,
                publication_seconds=0.0,
                analyze_seconds=0.0,
            )
            timings["total_seconds"] = round(time.monotonic() - started, 3)
            return {"status": "unchanged", **metadata, "timings": timings}
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            spool = Path(temporary) / "artifacts.jsonl"
            stage = time.monotonic()
            summary = prepare(lock, work / "packages", spool)
            timings["extraction_seconds"] = round(time.monotonic() - stage, 3)
            summary["lock_digest"] = digest(lock.model_dump())
            summary["roots"] = lock.roots
            summary["schema_version"] = db.SCHEMA_VERSION
            summary["embedding"] = lock.embedding
            stage = time.monotonic()
            if lock.embedding:
                summary["counts"].update(embeddings.prepare(work, spool, lock.embedding))
            timings["embedding_seconds"] = round(time.monotonic() - stage, 3)
            if (
                config_path.read_bytes() != config_bytes
                or (lock_path.read_bytes() if lock_path.exists() else None) != lock_bytes
            ):
                raise Error("Configuration or lock changed during sync; retry")
            # A failed publication may leave a pending lock, never mislabel old DB content.
            save_lock(lock_path, lock)
            stage = time.monotonic()
            publish(conn, lock, spool, identity, summary)
            timings["publication_seconds"] = round(time.monotonic() - stage, 3)
        stage = time.monotonic()
        conn.execute("ANALYZE")
        timings["analyze_seconds"] = round(time.monotonic() - stage, 3)
    timings["total_seconds"] = round(time.monotonic() - started, 3)
    return {"status": "synced", **summary, "timings": timings}
