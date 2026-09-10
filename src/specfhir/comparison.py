"""Published profile differences, not inferred FHIR compatibility or inheritance."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from specfhir import db, search
from specfhir.config import digest, split_key
from specfhir.models import Error, Result


def pointer_part(value):
    return value.replace("~", "~0").replace("/", "~1")


def value_evidence(row, pointer, present, value):
    evidence = {"present": present, "source": search.provenance(row, pointer)}
    if present:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True)
        if len(encoded) <= 2000:
            evidence["value"] = value
        else:
            evidence.update(
                value_preview=encoded[:2000],
                value_truncated=True,
                value_sha256=digest(value),
                value_json_characters=len(encoded),
            )
    return evidence


def differences(left, right, view):
    """Compare all fields; align only published element IDs, retain array ordering."""
    a, b = left["resource"], right["resource"]

    def fields(before, after, a_path, b_path, element_id=None):
        for field in sorted(before.keys() | after.keys()):
            a_has, b_has = field in before, field in after
            if a_has == b_has and digest(before.get(field)) == digest(after.get(field)):
                continue
            yield {
                "kind": "changed" if a_has and b_has else "removed" if a_has else "added",
                "element_id": element_id,
                "field": field,
                "before": value_evidence(
                    left, a_path + "/" + pointer_part(field), a_has, before.get(field)
                ),
                "after": value_evidence(
                    right, b_path + "/" + pointer_part(field), b_has, after.get(field)
                ),
            }

    yield from fields(
        {k: v for k, v in a.items() if k not in {"snapshot", "differential"}},
        {k: v for k, v in b.items() if k not in {"snapshot", "differential"}},
        "",
        "",
    )
    yield from fields(
        {k: v for k, v in a[view].items() if k != "element"},
        {k: v for k, v in b[view].items() if k != "element"},
        f"/{view}",
        f"/{view}",
    )
    a_elements = {item["id"]: (n, item) for n, item in enumerate(a[view]["element"])}
    b_elements = {item["id"]: (n, item) for n, item in enumerate(b[view]["element"])}
    # Compare order among shared IDs; inserted elements alone do not imply reordering.
    common = a_elements.keys() & b_elements.keys()
    a_order = [key for key in a_elements if key in common]
    b_order = [key for key in b_elements if key in common]
    if a_order != b_order:
        yield {
            "kind": "changed",
            "element_id": None,
            "field": "element_order",
            "before": value_evidence(left, f"/{view}/element", True, a_order),
            "after": value_evidence(right, f"/{view}/element", True, b_order),
            "value_description": "Order of shared IDs derived from the cited element arrays",
        }
    for key in sorted(a_elements.keys() | b_elements.keys()):
        old = a_elements.get(key)
        new = b_elements.get(key)
        if old is None or new is None:
            yield {
                "kind": "removed" if old else "added",
                "element_id": key,
                "field": None,
                "before": value_evidence(
                    left,
                    f"/{view}/element/{old[0]}" if old else f"/{view}/element",
                    old is not None,
                    old[1] if old else None,
                ),
                "after": value_evidence(
                    right,
                    f"/{view}/element/{new[0]}" if new else f"/{view}/element",
                    new is not None,
                    new[1] if new else None,
                ),
            }
        else:
            yield from fields(
                old[1], new[1], f"/{view}/element/{old[0]}", f"/{view}/element/{new[0]}", key
            )


def compare(
    selector: str,
    *,
    left_package: str,
    right_package: str,
    right_selector: str | None = None,
    left_artifact_version: str | None = None,
    right_artifact_version: str | None = None,
    view: str = "snapshot",
    element: str | None = None,
    offset: int = 0,
    limit: int = 50,
    dataset_id: str | None = None,
    config_path: Path = Path("specfhir.toml"),
) -> Result:
    """Compare exact published profiles; subsequent pages require dataset_id."""
    if not isinstance(selector, str) or not selector.strip():
        raise Error("Selector must contain 1–2048 characters")
    for package in (left_package, right_package):
        split_key(package)
    for value in (selector, right_selector):
        if value is not None and (not value.strip() or len(value) > 2048):
            raise Error("Selector must contain 1–2048 characters")
    if view not in {"snapshot", "differential"}:
        raise Error("view must be snapshot or differential")
    if element is not None and (not element or len(element) > 2048):
        raise Error("element must be a published ID with 1–2048 characters")
    with db.connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        state = db.published(conn)
        db.page_bounds(offset, limit, dataset_id, state["identity"])
        data: dict[str, Any] = {
            "view": view,
            "left_package": left_package,
            "right_package": right_package,
        }
        rows = []
        for side, package, name, version in (
            ("left", left_package, selector, left_artifact_version),
            ("right", right_package, right_selector, right_artifact_version),
        ):
            entry = conn.execute(
                "SELECT excluded_reason FROM packages WHERE key=%s", (package,)
            ).fetchone()
            if not entry or entry["excluded_reason"]:
                return Result(
                    dataset_id=state["identity"],
                    status="not_found",
                    data=data,
                    message=f"{side}: "
                    + (entry["excluded_reason"] if entry else "Package is not indexed"),
                )
            # Resolve the left alias once. Automatic pairing on the right is canonical-only.
            if name is None:
                name = rows[0]["canonical"]
                if not name:
                    raise Error("Left artifact has no canonical; supply right_selector explicitly")
            if "|" in name:
                name, embedded_version = name.rsplit("|", 1)
                if version and embedded_version != version:
                    raise Error(f"{side}: conflicting artifact version selectors")
                version = embedded_version
            matches = search.candidates(
                conn,
                package,
                name,
                version,
                canonical_only=side == "right" and right_selector is None,
            )
            if not matches:
                return Result(
                    dataset_id=state["identity"],
                    status="not_found",
                    data=data,
                    message=f"{side}: Artifact not found",
                )
            if len(matches) > 1:
                return Result(
                    dataset_id=state["identity"],
                    status="ambiguous",
                    data=data,
                    candidates=[search.provenance(r) for r in matches[:100]],
                    message=f"{side}: select an exact artifact; candidates capped at 100",
                )
            row = matches[0]
            data[side] = {
                "source": search.provenance(row),
                "ownership": "selected_package" if row["package_key"] == package else "dependency",
                "resource_sha256": digest(row["resource"]),
            }
            if row["resource_type"] != "StructureDefinition":
                raise Error(f"{side}: comparison currently supports StructureDefinition only")
            if row["projection_issues"].get(view) or not row["resource"].get(view, {}).get(
                "element"
            ):
                return Result(
                    dataset_id=state["identity"],
                    status="effective_definition_unavailable",
                    data=data,
                    message=f"{side}: Published {view} unavailable; inspect raw",
                )
            rows.append(row)
        if (rows[0]["package_key"] == left_package) != (rows[1]["package_key"] == right_package):
            raise Error("Pair changes package ownership; choose each owning package explicitly")
        counts = Counter()
        changes = []
        total = 0
        # ponytail: recompute per page; cache only if measured profile sizes require it.
        for change in differences(rows[0], rows[1], view):
            if element is not None and change["element_id"] != element:
                continue
            counts[change["kind"]] += 1
            if offset <= total < offset + limit:
                changes.append(change)
            total += 1
        if element is not None and not any(
            e["id"] == element for row in rows for e in row["resource"][view]["element"]
        ):
            return Result(
                dataset_id=state["identity"],
                status="not_found",
                data=data,
                message="Element ID is absent on both sides",
            )
        other_view = "differential" if view == "snapshot" else "snapshot"
        data.update(
            resource_changed=digest(rows[0]["resource"]) != digest(rows[1]["resource"]),
            unselected_representation_changed=digest(
                {k: v for k, v in rows[0]["resource"].items() if k == other_view}
            )
            != digest({k: v for k, v in rows[1]["resource"].items() if k == other_view}),
            element=element,
            changes=changes,
            counts=dict(counts),
            total=total,
            offset=offset,
            limit=limit,
            next_offset=offset + len(changes) if offset + len(changes) < total else None,
            limitations=[
                "Published source differences, not compatibility or inherited-field attribution",
                "The other representation is flagged but not compared",
                "Values over 2000 JSON characters have previews and hashes; "
                "inspect the cited raw artifact for full values",
            ],
        )
        return Result(dataset_id=state["identity"], status="ok", data=data)
