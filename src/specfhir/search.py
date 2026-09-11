"""Structured lookup and source-backed lexical/semantic retrieval for CLI and MCP."""

import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit

from specfhir import db, embeddings
from specfhir.config import load
from specfhir.models import RESOURCE_TYPES, Error, Result

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


def passage_context(resource, pointer):
    """Source links are evidence of a link, never proof of a change's explanation."""
    if resource.get("resourceType") != "Documentation":
        return {}
    fields = pointer.split("/")
    if len(fields) != 4 or fields[1] != "sections" or not fields[2].isdigit():
        return {}
    sections = resource.get("sections", [])
    i = int(fields[2])
    if i >= len(sections):
        return {}
    section = sections[i]
    url = resource["url"]
    links = []
    for href in section["links"]:
        try:
            target = urljoin(url, href)
            if urlsplit(target).scheme in {"http", "https"}:
                links.append(target)
        except ValueError:
            continue  # Original malformed href remains available in raw inspection.
    return {
        "heading": section["heading"],
        "anchor": section["anchor"],
        "citation_url": url + ("#" + quote(section["anchor"]) if section["anchor"] else ""),
        "source_sha256": resource["source_sha256"],
        "links": [{"url": link, "relationship": "published_link"} for link in links[:20]],
        "links_total": len(links),
        "links_unusable": len(section["links"]) - len(links),
        "links_truncated": len(links) > 20,
    }


def candidates(
    conn, context, selector, artifact_version=None, *, canonical_only=False, metadata_only=False
):
    columns = (
        "a.id,a.package_key,a.file_path,a.resource_type,a.resource_id,a.canonical,a.version"
        if metadata_only
        else "a.*"
    )
    rows = conn.execute(
        f"""
        {db.SCOPE}
        SELECT {columns} FROM artifacts a JOIN scope s ON a.package_key=s.key
        WHERE (a.canonical=%(selector)s OR (%(aliases)s AND
               (a.resource_id=%(selector)s OR a.name=%(selector)s OR a.name=%(profile_name)s)))
          AND (%(version)s::text IS NULL OR a.version=%(version)s)
        ORDER BY (a.package_key=%(package)s) DESC, a.package_key, a.file_path LIMIT 101
        """,
        {
            "package": context,
            "selector": selector,
            "aliases": not canonical_only,
            "profile_name": selector + "Profile",
            "version": artifact_version,
        },
    ).fetchall()
    # An explicit package picks its own matching definition before its dependency closure.
    direct = [row for row in rows if row["package_key"] == context]
    if direct:
        rows = direct
    return rows


def lookup(
    selector: str,
    *,
    package: str | None = None,
    artifact_version: str | None = None,
    element: str | None = None,
    view: Literal["snapshot", "differential", "raw", "passages", "incoming"] = "snapshot",
    pointer: str | None = None,
    offset: int = 0,
    limit: int = 5,
    config_path: Path = Path("specfhir.toml"),
    include_references: bool = False,
    dataset_id: str | None = None,
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
    if (
        "://" not in selector
        and not selector.lower().startswith("urn:")
        and "." in selector
        and element is None
    ):
        selector, element = selector.split(".", 1)
    if view not in {"snapshot", "differential", "raw", "passages", "incoming"}:
        raise Error("view must be snapshot, differential, raw, passages, or incoming")
    if view not in {"passages", "incoming"} and (pointer is not None or offset or limit != 5):
        raise Error("pointer, offset and limit require view=passages or incoming")
    if pointer is not None and (
        not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 2048
    ):
        raise Error("pointer must be a JSON pointer of at most 2048 characters")
    fragment = ""
    if view == "passages":
        if element is not None:
            raise Error("Use pointer, not element, with view=passages")
    elif view == "incoming" and (element is not None or pointer is not None):
        raise Error("element and pointer are not supported with view=incoming")
    with db.connect() as conn, conn.transaction():
        # Artifact and element reads must belong to the same published generation.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        state = db.published(conn)
        db.page_bounds(offset, limit, dataset_id, state["identity"])
        package_row = conn.execute("SELECT * FROM packages WHERE key=%s", (context,)).fetchone()
        if not package_row:
            return Result(
                dataset_id=state["identity"],
                status="not_found",
                context=context,
                message="Package is not indexed",
            )
        if package_row["excluded_reason"]:
            return Result(
                dataset_id=state["identity"],
                status="not_found",
                context=context,
                message=package_row["excluded_reason"],
            )
        rows = candidates(conn, context, selector, artifact_version)
        if not rows and view == "passages":
            page, fragment = urldefrag(selector)
            fragment = unquote(fragment)
            if fragment:
                rows = [
                    row
                    for row in candidates(conn, context, page, artifact_version)
                    if row["resource_type"] == "Documentation"
                ]
        if not rows:
            return Result(
                dataset_id=state["identity"],
                status="not_found",
                context=context,
                message="Artifact not found",
            )
        if len(rows) > 1:
            return Result(
                dataset_id=state["identity"],
                status="ambiguous",
                context=context,
                candidates=[provenance(row) for row in rows[:100]],
                message="Select an exact package/version; candidates capped at 100",
            )
        row = rows[0]
        resource = row["resource"]
        source = provenance(row)
        if view == "incoming":
            from specfhir.references import incoming

            return Result(
                status="ok",
                context=context,
                dataset_id=state["identity"],
                data={
                    "source": source,
                    "incoming_references": incoming(
                        conn, row, state["metadata"], context, offset, limit
                    ),
                },
            )
        if view == "passages":
            if fragment:
                matched = [
                    i
                    for i, section in enumerate(resource.get("sections", []))
                    if section.get("anchor") == fragment
                ]
                if len(matched) != 1:
                    return Result(
                        status="ambiguous" if matched else "not_found",
                        dataset_id=state["identity"],
                        context=context,
                        message="Publication anchor is missing or ambiguous",
                    )
                selected_pointer = f"/sections/{matched[0]}/text"
                if pointer is not None and pointer != selected_pointer:
                    raise Error("Conflicting pointer and publication anchor")
                pointer = selected_pointer
            count = conn.execute(
                "SELECT count(*) AS n FROM documents WHERE artifact_id=%s "
                "AND (%s::text IS NULL OR pointer=%s)",
                (row["id"], pointer, pointer),
            ).fetchone()
            assert count is not None
            total = count["n"]
            passages = conn.execute(
                "SELECT pointer,chunk,text FROM documents WHERE artifact_id=%s "
                "AND (%s::text IS NULL OR pointer=%s) "
                "ORDER BY CASE WHEN pointer LIKE '/sections/%%/text' "
                "THEN split_part(pointer,'/',3)::integer END, pointer,chunk LIMIT %s OFFSET %s",
                (row["id"], pointer, pointer, limit, offset),
            ).fetchall()
            return Result(
                status="ok" if total else "not_found",
                context=context,
                dataset_id=state["identity"],
                data={
                    "source": source,
                    "pointer": pointer,
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "next_offset": offset + len(passages)
                    if offset + len(passages) < total
                    else None,
                    "passages": [
                        {
                            "source": provenance(row, d["pointer"]),
                            "chunk": d["chunk"],
                            "text": d["text"],
                            **passage_context(resource, d["pointer"]),
                        }
                        for d in passages
                    ],
                },
            )
        reference_data = {}
        if include_references:
            from specfhir.references import inspection

            reference_data = {
                "references": inspection(conn, row["id"], state["metadata"], row["resource_type"])
            }
        if view == "raw" and element is None:
            return Result(
                dataset_id=state["identity"],
                status="ok",
                context=context,
                data={"source": source, "resource": resource, **reference_data},
            )
        if view == "raw":
            raise Error("Use snapshot or differential when selecting an element")
        if resource["resourceType"] == "StructureDefinition":
            if issue := row["projection_issues"].get(view):
                return Result(
                    dataset_id=state["identity"],
                    status="effective_definition_unavailable",
                    context=context,
                    data={"source": source, **reference_data},
                    message=f"Invalid {view}: {issue}; inspect raw",
                )
            entries = resource.get(view, {}).get("element", [])
            if not entries and view == "snapshot":
                return Result(
                    dataset_id=state["identity"],
                    status="effective_definition_unavailable",
                    context=context,
                    data={"source": source, **reference_data},
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
                        dataset_id=state["identity"],
                        status="not_found",
                        context=context,
                        data={"source": source, **reference_data},
                        message="Element not found in selected representation",
                    )
                if len(matches) > 1:
                    return Result(
                        dataset_id=state["identity"],
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
                    dataset_id=state["identity"],
                    status="ok",
                    context=context,
                    data={
                        "source": provenance(row, f"/{view}/element/{match['ordinal']}"),
                        **reference_data,
                        "representation": view,
                        "element": fields,
                    },
                )
        elif element:
            return Result(
                dataset_id=state["identity"],
                status="not_found",
                context=context,
                message="Artifact has no profile elements",
            )
        data: dict[str, Any] = {
            "source": source,
            **reference_data,
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
        return Result(dataset_id=state["identity"], status="ok", context=context, data=data)


def resolve(selector: str, **kwargs) -> Result:
    return lookup(selector, **kwargs)


def inspect(selector: str, **kwargs) -> Result:
    return lookup(selector, include_references=True, **kwargs)


def search(
    query: str,
    *,
    package: str | None = None,
    resource_type: str | None = None,
    limit: int = 5,
    mode: str = "auto",
    dataset_id: str | None = None,
    config_path: Path = Path("specfhir.toml"),
) -> Result:
    """Retrieve evidence in one locked package context; never generate an answer."""
    if not query.strip() or len(query) > 500:
        raise Error("Query must contain 1–500 nonblank characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise Error("limit must be an integer between 1 and 50")
    if resource_type is not None and resource_type not in RESOURCE_TYPES | {"Documentation"}:
        raise Error("Unsupported resource_type filter")
    if mode not in {"auto", "lexical", "semantic", "hybrid"}:
        raise Error("mode must be auto, lexical, semantic, or hybrid")
    context = package or load(config_path).default_package
    with db.connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        state = db.published(conn)
        db.page_bounds(0, limit, dataset_id, state["identity"])
        selected = conn.execute("SELECT * FROM packages WHERE key=%s", (context,)).fetchone()
        if not selected or selected["excluded_reason"]:
            return Result(
                status="not_found",
                dataset_id=state["identity"],
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
        rows = []
        if mode != "semantic":
            rows = conn.execute(
                f"""
                {db.SCOPE}, q AS (SELECT websearch_to_tsquery('english', %(query)s) AS terms),
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
                        ORDER BY (package_key=%(package)s) DESC,
                                 score DESC,package_key,pointer,chunk
                    ) AS duplicate FROM candidates
                )
                SELECT *, left(text,1000) AS excerpt, length(text)>1000 AS truncated
                FROM dedup WHERE duplicate=1
                ORDER BY (package_key=%(package)s) DESC,
                         score DESC,package_key,file_path,pointer,chunk
                LIMIT %(limit)s
            """,
                {
                    "package": context,
                    "query": query,
                    "type": resource_type,
                    "limit": candidate_limit,
                },
            ).fetchall()
        if mode in {"semantic", "hybrid"}:
            values = embeddings.query_vector(config_path.resolve().parent / ".specfhir", pin, query)
            # ponytail: exact scoped scan; add ANN only if measured query latency requires it.
            # Keep exact deduplication sorts in memory for the measured local package graph.
            conn.execute("SET LOCAL work_mem = '64MB'")
            semantic = conn.execute(
                f"""
                {db.SCOPE}, candidates AS (
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
            f"""
            {db.SCOPE} SELECT p.key,p.excluded_reason FROM packages p JOIN scope s USING(key)
              WHERE excluded_reason IS NOT NULL ORDER BY p.key
        """,
            {"package": context},
        ).fetchall()
        resources = {
            r["id"]: r["resource"]
            for r in conn.execute(
                "SELECT id,resource FROM artifacts WHERE id=ANY(%s) "
                "AND resource_type='Documentation'",
                ([r["artifact_id"] for r in rows],),
            ).fetchall()
        }
        return Result(
            status="ok",
            dataset_id=state["identity"],
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
                        "relationship": "relevance_candidate",
                        **passage_context(resources.get(row["artifact_id"], {}), row["pointer"]),
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
