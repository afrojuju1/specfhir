"""Explicit reference findings produced by sync; no FHIR validation or network resolution."""

import json
from collections import Counter
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from specfhir import search

RELATIONSHIPS = (
    "baseDefinition",
    "profile",
    "targetProfile",
    "binding.valueSet",
    "compose.valueSet",
    "contentReference",
)


def extract(resource, issues):
    """Yield source pointer, relationship, literal target, and optional local finding."""
    if resource["resourceType"] == "StructureDefinition":
        if isinstance(resource.get("baseDefinition"), str):
            yield ["/baseDefinition", "baseDefinition", resource["baseDefinition"], None]
        snapshot = (
            resource.get("snapshot", {}).get("element", []) if "snapshot" not in issues else []
        )
        for view in ("snapshot", "differential"):
            if view in issues:
                continue
            elements = resource.get(view, {}).get("element", [])
            target_view = "snapshot" if snapshot else view
            targets = snapshot or elements
            positions = {}
            for i, element in enumerate(targets):
                positions.setdefault(element["id"], []).append(i)
            for i, element in enumerate(elements):
                pointer = f"/{view}/element/{i}"
                target = element.get("contentReference")
                if isinstance(target, str):
                    finding = {
                        "status": "unsupported",
                        "reason": "Only local element contentReference targets are checked",
                    }
                    if target.startswith("#"):
                        matches = positions.get(target[1:], [])
                        status = (
                            "resolved"
                            if len(matches) == 1
                            else "ambiguous"
                            if matches
                            else "not_found_in_scope"
                            if snapshot
                            else "unsupported"
                        )
                        finding = {
                            "status": status,
                            "target_pointers": [
                                f"/{target_view}/element/{n}" for n in matches[:10]
                            ],
                            "reason": "Local element ID lookup"
                            if snapshot or matches
                            else "No usable snapshot; absence in a differential is inconclusive",
                        }
                    yield [pointer + "/contentReference", "contentReference", target, finding]
                binding = element.get("binding", {})
                if isinstance(binding, dict) and isinstance(binding.get("valueSet"), str):
                    yield [
                        pointer + "/binding/valueSet",
                        "binding.valueSet",
                        binding["valueSet"],
                        None,
                    ]
                types = element.get("type", [])
                for j, kind in enumerate(types if isinstance(types, list) else []):
                    if not isinstance(kind, dict):
                        continue
                    for relationship in ("profile", "targetProfile"):
                        values = kind.get(relationship, [])
                        for k, value in enumerate(values if isinstance(values, list) else []):
                            if isinstance(value, str):
                                yield [
                                    f"{pointer}/type/{j}/{relationship}/{k}",
                                    relationship,
                                    value,
                                    None,
                                ]
    if resource["resourceType"] == "ValueSet":
        compose = resource.get("compose", {})
        if not isinstance(compose, dict):
            return
        for clause in ("include", "exclude"):
            concepts = compose.get(clause, [])
            for i, concept in enumerate(concepts if isinstance(concepts, list) else []):
                if not isinstance(concept, dict):
                    continue
                values = concept.get("valueSet", [])
                for j, value in enumerate(values if isinstance(values, list) else []):
                    if isinstance(value, str):
                        yield [
                            f"/compose/{clause}/{i}/valueSet/{j}",
                            "compose.valueSet",
                            value,
                            None,
                        ]


def resolve(conn, package, target):
    """Use lookup's scope/version/own-package preference, without name aliases."""
    canonical, separator, version = target.partition("|")
    try:
        scheme = urlsplit(canonical).scheme
    except ValueError:
        scheme = ""
    if (
        not scheme
        or "#" in target
        or target.count("|") > 1
        or (separator and not version)
        or any(c.isspace() for c in target)
    ):
        return {
            "status": "unsupported",
            "reason": "Absolute canonical required; fragments are not traversed",
        }
    version = version if separator else None
    matches = search.candidates(
        conn, package, canonical, version, canonical_only=True, metadata_only=True
    )
    if matches:
        return {
            "status": "resolved" if len(matches) == 1 else "ambiguous",
            "candidates": [search.provenance(row) for row in matches[:10]],
            "candidates_truncated": len(matches) > 10,
            "reason": "Exact canonical lookup in source package and its dependencies",
        }
    excluded = conn.execute(
        """
        WITH RECURSIVE scope(key) AS (
            SELECT %s::text UNION
            SELECT dependency_key FROM package_dependencies d JOIN scope s ON d.package_key=s.key
        ) SELECT e.* FROM excluded_artifacts e JOIN scope s ON e.package_key=s.key
        WHERE canonical=%s AND (%s::text IS NULL OR version=%s)
        ORDER BY package_key,file_path LIMIT 11
    """,
        (package, canonical, version, version),
    ).fetchall()
    if excluded:
        return {
            "status": "excluded",
            "reason": "Matching scoped definitions are excluded from retrieval",
            "candidates": excluded[:10],
            "candidates_truncated": len(excluded) > 10,
        }
    elsewhere = conn.execute(
        """
        SELECT package_key,file_path,canonical,version FROM artifacts
        WHERE canonical=%s AND (%s::text IS NULL OR version=%s)
        UNION ALL SELECT package_key,file_path,canonical,version FROM excluded_artifacts
        WHERE canonical=%s AND (%s::text IS NULL OR version=%s)
        ORDER BY package_key,file_path LIMIT 11
    """,
        (canonical, version, version, canonical, version, version),
    ).fetchall()
    if elsewhere:
        return {
            "status": "outside_scope",
            "reason": "Matching identities exist outside the source dependency closure",
            "candidates": elsewhere[:10],
            "candidates_truncated": len(elsewhere) > 10,
        }
    return {
        "status": "not_found_in_scope",
        "reason": "No exact identity found locally; no network lookup performed",
    }


def publish(conn, spool, inventory):
    # Resolve each distinct target once; COPY cannot share a connection with active queries.
    outcomes = {}
    with spool.with_suffix(".references").open() as stream:
        for line in stream:
            _, package, _, _, target, local = json.loads(line)
            if local is None and (package, target) not in outcomes:
                outcomes[package, target] = resolve(conn, package, target)
    totals: Counter[str] = Counter()
    per_package = {item["key"]: Counter() for item in inventory}
    with (
        conn.cursor().copy("COPY artifact_references FROM STDIN") as copy,
        spool.with_suffix(".references").open() as stream,
    ):
        for line in stream:
            aid, package, pointer, relationship, target, local = json.loads(line)
            outcome = local if local is not None else outcomes[package, target]
            status = outcome["status"]
            totals[status] += 1
            per_package[package][status] += 1
            copy.write_row((aid, pointer, relationship, target, status, Jsonb(outcome)))
    for item in inventory:
        item["reference_counts"] = dict(per_package[item["key"]])
    return {
        "status": "completed",
        "counts": dict(totals),
        "relationships": list(RELATIONSHIPS),
        "scope": "StructureDefinition and ValueSet references; each source occurrence is counted",
        "limitations": "Not conformance validation; only the listed relationships are checked.",
        "not_checked": [
            "malformed projections and fields",
            "contained canonical fragments",
            "external element references",
            "instance references",
            "HTML hyperlinks",
            "other resource relationships",
        ],
    }


def inspection(conn, artifact_id, metadata, resource_type):
    if "reference_checks" not in metadata:
        return {"status": "unavailable", "message": "Run sync to publish reference findings"}
    if resource_type not in {"StructureDefinition", "ValueSet"}:
        return {
            "status": "not_checked",
            "reason": "This resource type is outside reference coverage",
        }
    counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status,count(*) FROM artifact_references WHERE artifact_id=%s GROUP BY status",
            (artifact_id,),
        ).fetchall()
    }
    items = conn.execute(
        """
        SELECT pointer,relationship,target,status,detail FROM artifact_references
        WHERE artifact_id=%s ORDER BY (status='resolved'),pointer LIMIT 100
    """,
        (artifact_id,),
    ).fetchall()
    return {
        "status": "completed",
        "counts": counts,
        "items": items,
        "truncated": sum(counts.values()) > len(items),
        "limit": 100,
        "index_lock_digest": metadata["lock_digest"],
        "relationships": metadata["reference_checks"]["relationships"],
        "not_checked": metadata["reference_checks"]["not_checked"],
    }
