"""Published profile differences, not inferred FHIR compatibility or inheritance."""

import json
from collections import Counter, defaultdict
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from specfhir import db, references, search
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
    selector: str | None = None,
    *,
    left_package: str,
    right_package: str,
    mode: str = "profile",
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
    """Compare published sources in explicit contexts; continuation requires dataset_id."""
    if mode not in {"profile", "package", "references"}:
        raise Error("mode must be profile, package or references")
    if mode == "package" and any(
        v is not None
        for v in (selector, right_selector, left_artifact_version, right_artifact_version, element)
    ):
        raise Error("Package comparison does not accept artifact or element selectors")
    if mode != "package" and (not isinstance(selector, str) or not selector.strip()):
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
        if mode == "package":
            return package_comparison(conn, state, data, offset, limit)
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
            if mode == "references":
                if row["resource_type"] not in {"StructureDefinition", "ValueSet"}:
                    raise Error("Reference comparison supports StructureDefinition and ValueSet")
                rows.append(row)
                continue
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
        if mode == "references":
            return reference_comparison(conn, state, data, rows, view, element, offset, limit)
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


def comparison_page(state, data, items, offset, limit):
    """One deterministic page, with complete counts even when unchanged items dominate."""
    counts = defaultdict(Counter)
    for item in items:
        counts[item["category"]][item["kind"]] += 1
    data.update(
        items=items[offset : offset + limit],
        counts={key: dict(value) for key, value in sorted(counts.items())},
        total=len(items),
        offset=offset,
        limit=limit,
        next_offset=offset + limit if offset + limit < len(items) else None,
    )
    return Result(status="ok", dataset_id=state["identity"], data=data)


def package_comparison(conn, state, data, offset, limit):
    inventories = []
    graphs = []
    for side in ("left", "right"):
        package = data[side + "_package"]
        entry = conn.execute("SELECT * FROM packages WHERE key=%s", (package,)).fetchone()
        if not entry or entry["excluded_reason"]:
            return Result(
                status="not_found",
                dataset_id=state["identity"],
                data=data,
                message=f"{side}: "
                + (entry["excluded_reason"] if entry else "Package is not indexed"),
            )
        owned = defaultdict(list)
        # Stream large resources; retain only identity and hashes, never whole package JSON.
        # ponytail: hash per page; persist hashes if large core comparisons justify it.
        with conn.cursor(name=f"owned_{side}") as cursor:
            cursor.execute(
                "SELECT * FROM artifacts WHERE package_key=%s ORDER BY file_path", (package,)
            )
            for row in cursor:
                identity = (
                    row["resource_type"],
                    "canonical" if row["canonical"] else "id" if row["resource_id"] else "file",
                    row["canonical"] or row["resource_id"] or row["file_path"],
                )
                owned[identity].append(
                    {
                        "source": search.provenance(row),
                        "resource_sha256": digest(row["resource"]),
                        "profile_details_available": row["resource_type"] == "StructureDefinition"
                        and not row["projection_issues"].get(data["view"])
                        and bool(row["resource"].get(data["view"], {}).get("element")),
                    }
                )
        for row in conn.execute(
            "SELECT * FROM excluded_artifacts WHERE package_key=%s ORDER BY file_path", (package,)
        ):
            owned[row["resource_type"], "canonical", row["canonical"]].append(
                {
                    "source": {
                        "package": package,
                        "file": row["file_path"],
                        "canonical": row["canonical"],
                        "artifact_version": row["version"],
                        "resource_type": row["resource_type"],
                    },
                    "excluded_reason": row["reason"],
                }
            )
        inventories.append(owned)
        graph = conn.execute(
            f"{db.SCOPE} SELECT d.* FROM package_dependencies d JOIN scope s ON "
            "d.package_key=s.key ORDER BY package_key,dependency_key",
            {"package": package},
        ).fetchall()
        graphs.append(
            {
                (
                    "$root" if edge["package_key"] == package else edge["package_key"],
                    edge["dependency_key"],
                ): edge
                for edge in graph
            }
        )
        data[side] = {
            "package": package,
            "archive_sha256": entry["sha256"],
            "artifact_records": sum(map(len, owned.values())),
        }
        topology = TopologicalSorter()
        for edge in graph:
            topology.add(edge["package_key"], edge["dependency_key"])
        try:
            topology.prepare()
            data[side]["dependency_cycle"] = False
        except CycleError:
            data[side]["dependency_cycle"] = True
    items = []
    a, b = inventories
    for key in sorted(a.keys() | b.keys()):
        old, new = a.get(key, []), b.get(key, [])
        kind = (
            "uncomparable"
            if len(old) > 1 or len(new) > 1 or any("excluded_reason" in r for r in old + new)
            else "added"
            if not old
            else "removed"
            if not new
            else "unchanged"
            if old[0]["resource_sha256"] == new[0]["resource_sha256"]
            else "changed"
        )
        items.append(
            {
                "category": "artifact",
                "identity": list(key),
                "kind": kind,
                "before": old[:10],
                "after": new[:10],
                "before_count": len(old),
                "after_count": len(new),
                "candidates_truncated": max(len(old), len(new)) > 10,
            }
        )
    # Exact edges preserve attribution; only the selected root's name is normalized.
    a_graph, b_graph = graphs
    for key in sorted(a_graph.keys() | b_graph.keys()):
        items.append(
            {
                "category": "dependency_edge",
                "kind": "unchanged"
                if key in a_graph and key in b_graph
                else "removed"
                if key in a_graph
                else "added",
                "before": a_graph.get(key),
                "after": b_graph.get(key),
            }
        )
    pins = []
    for side in ("left", "right"):
        by_name = defaultdict(list)
        for row in conn.execute(
            f"{db.SCOPE} SELECT p.key,p.excluded_reason FROM packages p JOIN scope s "
            "USING(key) WHERE p.key<>%(package)s ORDER BY p.key",
            {"package": data[side + "_package"]},
        ):
            by_name[split_key(row["key"])[0]].append(row)
        pins.append(by_name)
    for name in sorted(pins[0].keys() | pins[1].keys()):
        old, new = pins[0].get(name, []), pins[1].get(name, [])
        items.append(
            {
                "category": "dependency_pin",
                "name": name,
                "kind": "added"
                if not old
                else "removed"
                if not new
                else "unchanged"
                if old == new
                else "changed",
                "before": old[:10],
                "after": new[:10],
                "before_count": len(old),
                "after_count": len(new),
                "candidates_truncated": max(len(old), len(new)) > 10,
            }
        )
    data.update(
        mode="package",
        limitations=[
            "Owned indexed artifacts and inventoried exclusions only; dependency "
            "artifacts never substitute owned artifacts",
            "Match resource type and canonical, otherwise exact resource ID or file; no "
            "inferred renames",
            "Changed means source JSON differs, not compatibility; ambiguous or excluded "
            "identities are uncomparable",
            "Dependency pins include the exact closure; edges retain declaring package. "
            "Cycles terminate through set closure",
            "Candidate lists are capped at 10 with complete counts; inspect an exact "
            "package and artifact for details",
        ],
    )
    return comparison_page(state, data, items, offset, limit)


def reference_comparison(conn, state, data, rows, view, element, offset, limit):
    if "reference_checks" not in state["metadata"]:
        raise Error("Reference findings unavailable; run sync")
    sides = []
    for row in rows:
        if row["resource_type"] == "StructureDefinition" and (
            row["projection_issues"].get(view) or not row["resource"].get(view, {}).get("element")
        ):
            return Result(
                status="effective_definition_unavailable",
                dataset_id=state["identity"],
                data=data,
                message=f"Published {view} unavailable; inspect raw",
            )
        occurrences = defaultdict(list)
        for ref in conn.execute(
            "SELECT * FROM artifact_references WHERE artifact_id=%s ORDER BY pointer", (row["id"],)
        ):
            parts = ref["pointer"].split("/")
            element_id = None
            if len(parts) > 3 and parts[1] in {"snapshot", "differential"}:
                if parts[1] != view:
                    continue
                element_id = row["resource"][view]["element"][int(parts[3])]["id"]
            if element is not None and element_id != element:
                continue
            # Match repeated literals as a group, not guessed array positions across releases.
            key = (
                element_id or "",
                ref["relationship"],
                ref["target"],
                parts[2] if parts[1] == "compose" else "",
            )
            occurrences[key].append(ref)
        sides.append(occurrences)
    if element is not None and not any(
        e.get("id") == element
        for row in rows
        for e in row["resource"].get(view, {}).get("element", [])
    ):
        return Result(
            status="not_found",
            dataset_id=state["identity"],
            data=data,
            message="Element ID is absent on both sides",
        )

    def target_evidence(row, ref):
        detail = dict(ref["detail"])
        result = {"status": ref["status"], "detail": detail}
        if ref["status"] != "resolved":
            return result
        if ref["relationship"] == "contentReference":
            pointer = detail["target_pointers"][0]
            parts = pointer.split("/")
            target = row["resource"][parts[1]]["element"][int(parts[3])]
            result.update(
                source=search.provenance(row, pointer),
                content_sha256=digest(target),
                self_cycle=ref["pointer"].rsplit("/", 1)[0] == pointer,
            )
        else:
            source = detail["candidates"][0]
            target_row = conn.execute(
                "SELECT * FROM artifacts WHERE package_key=%s AND file_path=%s",
                (source["package"], source["file"]),
            ).fetchone()
            if not target_row:
                raise Error("Published reference target is missing; run sync")
            result.update(
                source=source,
                content_sha256=digest(target_row["resource"]),
                self_cycle=target_row["id"] == row["id"],
            )
        result["traversal"] = "cycle" if result["self_cycle"] else "depth_limit"
        return result

    items = []
    for key in sorted(sides[0].keys() | sides[1].keys()):
        evidence = []
        for row, occurrences in zip(rows, sides, strict=True):
            refs = occurrences.get(key, [])
            evidence.append(
                None
                if not refs
                else {
                    "occurrences": len(refs),
                    "sources": [search.provenance(row, ref["pointer"]) for ref in refs[:10]],
                    "sources_truncated": len(refs) > 10,
                    "target": target_evidence(row, refs[0]),
                }
            )
        old, new = evidence
        kind = (
            "added"
            if old is None
            else "removed"
            if new is None
            else "uncomparable"
            if old["target"]["status"] != "resolved" or new["target"]["status"] != "resolved"
            else "changed"
            if (old["target"]["source"], old["target"]["content_sha256"])
            != (new["target"]["source"], new["target"]["content_sha256"])
            else "unchanged"
        )
        items.append(
            {
                "category": "reference",
                "element_id": key[0] or None,
                "relationship": key[1],
                "literal": key[2],
                "compose_clause": key[3] or None,
                "kind": kind,
                "literal_unchanged": old is not None and new is not None,
                "occurrence_count_changed": old is not None
                and new is not None
                and old["occurrences"] != new["occurrences"],
                "target_content_changed": old["target"].get("content_sha256")
                != new["target"].get("content_sha256")
                if old and new and kind != "uncomparable"
                else None,
                "before": old,
                "after": new,
            }
        )
    data.update(
        mode="references",
        resource_changed=digest(rows[0]["resource"]) != digest(rows[1]["resource"]),
        max_depth=1,
        relationships=list(references.RELATIONSHIPS),
        not_checked=state["metadata"]["reference_checks"]["not_checked"],
        limitations=[
            "Direct targets only, resolved in each source artifact's exact package "
            "closure at publication",
            "Self cycles are marked; longer cycles and downstream impacts are not "
            "traversed at depth 1",
            "Group identical literals by element ID and relationship (ValueSet "
            "include/exclude retained); no inferred pairing of changed literals",
            "A target identity or content change is evidence, not proof of behavior "
            "change or inherited authorship",
            "Source occurrences are capped at 10 per group with complete counts",
        ],
    )
    return comparison_page(state, data, items, offset, limit)
