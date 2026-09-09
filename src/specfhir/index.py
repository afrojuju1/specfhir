"""Prepare off-transaction, publish atomically, preserve prior successful state."""

import json
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
}


def prepare(lock: Lock, cache: Path, spool: Path) -> dict[str, Any]:
    inventory = []
    counts: Counter[str] = Counter()
    with spool.open("w") as output, spool.with_suffix(".documents").open("w") as docs:
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
                if resource.get("fhirVersion", "4.0.1") != "4.0.1":
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
                    "excluded_reason": reason,
                    "artifacts": package_count,
                    "skipped": dict(skipped),
                }
            )
    return {"inventory": inventory, "counts": dict(counts)}


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
            for dep in pin.dependencies:
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
        with conn.cursor().copy("COPY elements FROM STDIN") as copy, spool.open() as stream:
            for line in stream:
                aid, _, _, resource, issues = json.loads(line)
                if resource["resourceType"] != "StructureDefinition":
                    continue
                for view in ("snapshot", "differential"):
                    if view in issues:
                        continue
                    for ordinal, element in enumerate(resource.get(view, {}).get("element", [])):
                        copy.write_row(
                            (
                                aid,
                                view,
                                element["id"],
                                element["path"],
                                element.get("sliceName"),
                                ordinal,
                                Jsonb(element),
                            )
                        )
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
    config_path = config_path.resolve()
    work = config_path.parent / ".specfhir"
    work.mkdir(exist_ok=True)
    lock_path = config_path.with_name("specfhir.lock")
    with db.connect() as conn:
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
        lock = resolve_lock(config, work / "packages", previous)
        if config.embedding.enabled:
            lock.embedding = embeddings.pin_model(
                work, config.embedding, existing.embedding if existing else None, update_lock
            )
        identity = digest({"lock": lock.model_dump(), "schema": db.SCHEMA_VERSION})
        conn.execute(db.DDL)
        state = conn.execute("SELECT * FROM index_state").fetchone()
        if state and state["identity"] == identity:
            if previous is None:
                save_lock(lock_path, lock)
            return {"status": "unchanged", **state["metadata"]}
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            spool = Path(temporary) / "artifacts.jsonl"
            summary = prepare(lock, work / "packages", spool)
            summary["lock_digest"] = digest(lock.model_dump())
            summary["roots"] = lock.roots
            summary["schema_version"] = db.SCHEMA_VERSION
            summary["embedding"] = lock.embedding
            if lock.embedding:
                summary["counts"].update(embeddings.prepare(work, spool, lock.embedding))
            summary["preparation_seconds"] = round(time.monotonic() - started, 3)
            if (
                config_path.read_bytes() != config_bytes
                or (lock_path.read_bytes() if lock_path.exists() else None) != lock_bytes
            ):
                raise Error("Configuration or lock changed during sync; retry")
            # A failed publication may leave a pending lock, never mislabel old DB content.
            save_lock(lock_path, lock)
            publish(conn, lock, spool, identity, summary)
        conn.execute("ANALYZE")
    return {"status": "synced", **summary}
