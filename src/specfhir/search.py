"""Structured lookup and source-backed lexical/semantic retrieval for CLI and MCP."""

import json
from pathlib import Path
from typing import Any, Literal

from specfhir import db, embeddings
from specfhir.config import load
from specfhir.models import Error, Result

ELEMENT_FIELDS = (
    "id",
    "path",
    "sliceName",
    "min",
    "max",
    "type",
    "mustSupport",
    "binding",
    "constraint",
    "slicing",
    "short",
    "definition",
    "comment",
    "requirements",
    "contentReference",
    "isModifier",
    "isModifierReason",
    "condition",
    "base",
)
ARTIFACT_FIELDS = (
    "resourceType",
    "id",
    "url",
    "version",
    "name",
    "title",
    "status",
    "description",
    "purpose",
    "fhirVersion",
    "kind",
    "type",
    "baseDefinition",
    "derivation",
    "code",
    "base",
    "expression",
    "target",
    "binding",
    "content",
    "valueSet",
)


def provenance(row: dict[str, Any], pointer: str = "") -> dict[str, Any]:
    return {
        "package": row["package_key"],
        "fhir_release": "4.0.1",
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "canonical": row["canonical"],
        "artifact_version": row["version"],
        "file": row["file_path"],
        "pointer": pointer,
    }


def lookup(
    selector: str,
    *,
    package: str | None = None,
    artifact_version: str | None = None,
    element: str | None = None,
    view: Literal["snapshot", "differential", "raw"] = "snapshot",
    config_path: Path = Path("specfhir.toml"),
) -> Result:
    if not selector or len(selector) > 2048:
        raise Error("Selector must contain 1–2048 characters")
    context = package or load(config_path).default_package
    if "|" in selector:
        selector, version = selector.rsplit("|", 1)
        if artifact_version and version != artifact_version:
            raise Error("Conflicting artifact version selectors")
        artifact_version = version
    # URLs contain dots and are never interpreted as dotted shorthand.
    if "://" not in selector and "." in selector and element is None:
        selector, element = selector.split(".", 1)
    if view not in {"snapshot", "differential", "raw"}:
        raise Error("view must be snapshot, differential, or raw")
    with db.connect() as conn, conn.transaction():
        # Artifact and element reads must belong to the same published generation.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        relation = conn.execute("SELECT to_regclass('index_state') AS relation").fetchone()
        if not relation or not relation["relation"]:
            raise Error("No index; run specfhir sync first")
        state = conn.execute("SELECT metadata FROM index_state").fetchone()
        if not state:
            raise Error("No successful sync; run specfhir sync first")
        package_row = conn.execute("SELECT * FROM packages WHERE key=%s", (context,)).fetchone()
        if not package_row:
            return Result(status="not_found", context=context, message="Package is not indexed")
        if package_row["excluded_reason"]:
            return Result(
                status="not_found", context=context, message=package_row["excluded_reason"]
            )
        rows = conn.execute(
            """
            WITH RECURSIVE scope(key) AS (
                SELECT key FROM packages WHERE key=%s
                UNION
                SELECT dependency_key FROM package_dependencies d
                JOIN scope s ON d.package_key=s.key
            )
            SELECT a.* FROM artifacts a JOIN scope s ON a.package_key=s.key
            WHERE (a.canonical=%s OR a.resource_id=%s OR a.name=%s OR a.name=%s)
              AND (%s::text IS NULL OR a.version=%s)
            ORDER BY (a.package_key=%s) DESC, a.package_key, a.file_path LIMIT 101
        """,
            (
                context,
                selector,
                selector,
                selector,
                selector + "Profile",
                artifact_version,
                artifact_version,
                context,
            ),
        ).fetchall()
        # An explicit package picks its own matching definition before its dependency closure.
        direct = [row for row in rows if row["package_key"] == context]
        if direct:
            rows = direct
        if not rows:
            return Result(status="not_found", context=context, message="Artifact not found")
        if len(rows) > 1:
            return Result(
                status="ambiguous",
                context=context,
                candidates=[provenance(row) for row in rows[:100]],
                message="Select an exact package/version; candidates capped at 100",
            )
        row = rows[0]
        resource = row["resource"]
        source = provenance(row)
        if view == "raw" and element is None:
            return Result(
                status="ok", context=context, data={"source": source, "resource": resource}
            )
        if view == "raw":
            raise Error("Use snapshot or differential when selecting an element")
        if resource["resourceType"] == "StructureDefinition":
            if issue := row["projection_issues"].get(view):
                return Result(
                    status="effective_definition_unavailable",
                    context=context,
                    data={"source": source},
                    message=f"Invalid {view}: {issue}; inspect raw",
                )
            entries = resource.get(view, {}).get("element", [])
            if not entries and view == "snapshot":
                return Result(
                    status="effective_definition_unavailable",
                    context=context,
                    data={"source": source},
                    message="No supplied snapshot; inspect differential or raw",
                )
            if element:
                root = resource.get("type", entries[0]["path"] if entries else "")
                path = (
                    element
                    if element == root or element.startswith(root + ".")
                    else f"{root}.{element}"
                )
                matches = conn.execute(
                    """
                    SELECT * FROM elements WHERE artifact_id=%s AND representation=%s
                    AND (element_id=%s OR path=%s) ORDER BY ordinal LIMIT 101
                """,
                    (row["id"], view, path, path),
                ).fetchall()
                if not matches:
                    return Result(
                        status="not_found",
                        context=context,
                        data={"source": source},
                        message="Element not found in selected representation",
                    )
                if len(matches) > 1:
                    return Result(
                        status="ambiguous",
                        context=context,
                        candidates=[
                            {
                                "element_id": match["element_id"],
                                "source": provenance(row, f"/{view}/element/{match['ordinal']}"),
                            }
                            for match in matches[:100]
                        ],
                        message="Select a slice by element ID; candidates capped at 100",
                    )
                match = matches[0]
                value = match["element"]
                fields = {
                    key: val
                    for key, val in value.items()
                    if key in ELEMENT_FIELDS or key.startswith(("fixed", "pattern"))
                }
                return Result(
                    status="ok",
                    context=context,
                    data={
                        "source": provenance(row, f"/{view}/element/{match['ordinal']}"),
                        "representation": view,
                        "element": fields,
                    },
                )
        elif element:
            return Result(
                status="not_found", context=context, message="Artifact has no profile elements"
            )
        data: dict[str, Any] = {
            "source": source,
            "artifact": {key: resource[key] for key in ARTIFACT_FIELDS if key in resource},
        }
        if resource["resourceType"] == "StructureDefinition":
            entries = resource.get(view, {}).get("element", [])
            data.update(
                {
                    "representation": view,
                    "element_count": len(entries),
                    "elements": [
                        {
                            key: item[key]
                            for key in ("id", "path", "sliceName", "min", "max", "mustSupport")
                            if key in item
                        }
                        for item in entries[:100]
                    ],
                    "truncated": len(entries) > 100,
                }
            )
        return Result(status="ok", context=context, data=data)


def resolve(selector: str, **kwargs) -> Result:
    return lookup(selector, **kwargs)


def inspect(selector: str, **kwargs) -> Result:
    return lookup(selector, **kwargs)


def search(
    query: str,
    *,
    package: str | None = None,
    resource_type: str | None = None,
    limit: int = 5,
    mode: str = "auto",
    config_path: Path = Path("specfhir.toml"),
) -> Result:
    """Retrieve evidence in one locked package context; never generate an answer."""
    if not query.strip() or len(query) > 500:
        raise Error("Query must contain 1–500 nonblank characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise Error("limit must be an integer between 1 and 50")
    if resource_type is not None and resource_type not in {
        "StructureDefinition",
        "SearchParameter",
        "ValueSet",
        "CodeSystem",
        "ConceptMap",
        "OperationDefinition",
        "ImplementationGuide",
    }:
        raise Error("Unsupported resource_type filter")
    if mode not in {"auto", "lexical", "semantic", "hybrid"}:
        raise Error("mode must be auto, lexical, semantic, or hybrid")
    context = package or load(config_path).default_package
    with db.connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        relation = conn.execute("SELECT to_regclass('index_state') AS relation").fetchone()
        if not relation or not relation["relation"]:
            raise Error("No index; run specfhir sync first")
        state = conn.execute("SELECT metadata FROM index_state").fetchone()
        if not state or state["metadata"].get("schema_version", 0) < 2:
            raise Error("Text index unavailable; run specfhir sync first")
        selected = conn.execute("SELECT * FROM packages WHERE key=%s", (context,)).fetchone()
        if not selected or selected["excluded_reason"]:
            return Result(
                status="not_found",
                context=context,
                message=selected["excluded_reason"] if selected else "Package is not indexed",
            )
        pin = state["metadata"].get("embedding")
        if mode == "auto":
            mode = "hybrid" if pin else "lexical"
        if mode in {"semantic", "hybrid"} and not pin:
            raise Error(
                "Semantic index unavailable; enable embeddings and run sync, or use lexical mode"
            )
        candidate_limit = limit if mode == "lexical" else 100
        rows = conn.execute(
            """
            WITH RECURSIVE scope(key) AS (
                SELECT key FROM packages WHERE key=%(package)s
                UNION
                SELECT dependency_key FROM package_dependencies d
                JOIN scope s ON d.package_key=s.key
            ), q AS (SELECT websearch_to_tsquery('english', %(query)s) AS terms),
            candidates AS (
                SELECT d.*, a.package_key,a.file_path,a.resource_type,a.resource_id,
                       a.canonical,a.version,
                       ts_rank_cd(d.search_vector,q.terms,32) AS score, 'fts' AS method
                FROM documents d JOIN artifacts a ON a.id=d.artifact_id
                JOIN scope s ON s.key=a.package_key CROSS JOIN q
                WHERE d.search_vector @@ q.terms
                  AND (%(type)s::text IS NULL OR a.resource_type=%(type)s)
                UNION ALL
                SELECT d.*, a.package_key,a.file_path,a.resource_type,a.resource_id,
                       a.canonical,a.version,
                       public.similarity(coalesce(a.name,'') || ' ' || coalesce(a.title,''),
                                         %(query)s) * 0.1 AS score, 'fuzzy' AS method
                FROM artifacts a JOIN scope s ON s.key=a.package_key
                JOIN LATERAL (
                    SELECT * FROM documents WHERE artifact_id=a.id
                    ORDER BY (kind='description') DESC, pointer, chunk LIMIT 1
                ) d ON true
                WHERE (coalesce(a.name,'') || ' ' || coalesce(a.title,''))
                      OPERATOR(public.%%) %(query)s
                  AND (%(type)s::text IS NULL OR a.resource_type=%(type)s)
            ), dedup AS (
                SELECT *, row_number() OVER (
                    PARTITION BY coalesce(canonical,resource_type || '/' || resource_id),
                                 version,element_id,text_hash
                    ORDER BY (package_key=%(package)s) DESC, score DESC,package_key,pointer,chunk
                ) AS duplicate FROM candidates
            )
            SELECT *, left(text,1000) AS excerpt, length(text)>1000 AS truncated
            FROM dedup WHERE duplicate=1
            ORDER BY (package_key=%(package)s) DESC, score DESC,package_key,file_path,pointer,chunk
            LIMIT %(limit)s
        """,
            {"package": context, "query": query, "type": resource_type, "limit": candidate_limit},
        ).fetchall()
        if mode in {"semantic", "hybrid"}:
            values = embeddings.query_vector(config_path.resolve().parent / ".specfhir", pin, query)
            # ponytail: exact scoped scan; add ANN only if measured query latency requires it.
            semantic = conn.execute(
                """
                WITH RECURSIVE scope(key) AS (
                    SELECT key FROM packages WHERE key=%(package)s UNION
                    SELECT dependency_key FROM package_dependencies d
                    JOIN scope s ON d.package_key=s.key
                ), candidates AS (
                    SELECT d.artifact_id,d.pointer,d.chunk,d.element_id,d.text_hash,
                           a.package_key,a.file_path,a.resource_type,a.resource_id,
                           a.canonical,a.version,
                           1 - (embedding OPERATOR(public.<=>) %(vector)s::public.vector) AS score,
                           'semantic' AS method
                    FROM documents d JOIN artifacts a ON a.id=d.artifact_id
                    JOIN scope s ON a.package_key=s.key
                    WHERE embedding IS NOT NULL
                      AND (%(type)s::text IS NULL OR a.resource_type=%(type)s)
                ), dedup AS (
                    SELECT *,row_number() OVER (
                        PARTITION BY coalesce(canonical,resource_type || '/' || resource_id),
                                     version,element_id,text_hash
                        ORDER BY (package_key=%(package)s) DESC,score DESC,package_key,pointer,chunk
                    ) AS duplicate FROM candidates
                ), ranked AS MATERIALIZED (
                    SELECT * FROM dedup WHERE duplicate=1
                    ORDER BY (package_key=%(package)s) DESC,score DESC,
                             package_key,file_path,pointer,chunk
                    LIMIT 100
                )
                SELECT r.*,d.kind,d.representation,left(d.text,1000) AS excerpt,
                       length(d.text)>1000 AS truncated
                FROM ranked r JOIN documents d USING (artifact_id,pointer,chunk)
                ORDER BY (r.package_key=%(package)s) DESC,r.score DESC,
                         r.package_key,r.file_path,r.pointer,r.chunk
            """,
                {"package": context, "vector": json.dumps(values), "type": resource_type},
            ).fetchall()
            rows = semantic[:limit] if mode == "semantic" else fuse(rows, semantic)[:limit]
        excluded = conn.execute(
            """
            WITH RECURSIVE scope(key) AS (
                SELECT key FROM packages WHERE key=%s UNION
                SELECT dependency_key FROM package_dependencies d
                JOIN scope s ON d.package_key=s.key
            ) SELECT p.key,p.excluded_reason FROM packages p JOIN scope s USING(key)
              WHERE excluded_reason IS NOT NULL ORDER BY p.key
        """,
            (context,),
        ).fetchall()
        return Result(
            status="ok",
            context=context,
            data={
                "query": query,
                "mode": mode,
                "embedding_model": pin["model"] if pin and mode != "lexical" else None,
                "limit": limit,
                "excluded_packages": excluded,
                "results": [
                    {
                        "source": provenance(row, row["pointer"]),
                        "element_id": row["element_id"],
                        "representation": row["representation"],
                        "kind": row["kind"],
                        "chunk": row["chunk"],
                        "text": row["excerpt"],
                        "truncated": row["truncated"],
                        "method": row["method"],
                        "score": float(row["score"]),
                    }
                    for row in rows
                ],
            },
        )


def fuse(lexical: list[dict], semantic: list[dict]) -> list[dict]:
    """Equal-weight reciprocal rank fusion over two bounded, deduplicated lists."""
    results = {}
    for candidates in (lexical, semantic):
        for rank, row in enumerate(candidates, 1):
            key = (
                row["canonical"] or row["resource_type"] + "/" + row["resource_id"],
                row["version"],
                row["element_id"],
                row["text_hash"],
            )
            if key not in results:
                results[key] = {**row, "score": 0.0, "method": "hybrid"}
            results[key]["score"] += 1 / (60 + rank)
    return sorted(
        results.values(),
        key=lambda row: (
            -row["score"],
            row["package_key"],
            row["file_path"],
            row["pointer"],
            row["chunk"],
        ),
    )
