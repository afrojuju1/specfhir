"""Exact structured retrieval only; prose search belongs to Phase 2."""

from pathlib import Path
from typing import Any, Literal

from specfhir import db
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
