import copy
import json

import pytest
from helpers import archive, pointer_value, profile, project_config
from typer.testing import CliRunner

from specfhir import comparison, db, index, packages, search, validator
from specfhir.cli import app
from specfhir.config import digest
from specfhir.models import Error


def test_profile_comparison_and_contexts(tmp_path, database, monkeypatch):
    left, right = "example#1.0.0", "example#2.0.0"
    cache = tmp_path / ".specfhir/packages"
    old = profile()
    old["snapshot"]["element"][1]["mustSupport"] = False
    new = copy.deepcopy(old)
    new["version"] = "2.0.0"
    new["description"] = "long guidance " * 300
    new["snapshot"]["element"][2]["min"] = 0
    new["snapshot"]["element"][1]["mustSupport"] = None
    new["snapshot"]["element"].pop(3)
    new["snapshot"]["element"].append({"id": "Patient.active", "path": "Patient.active", "min": 0})
    new["snapshot"]["element"][1:3] = reversed(new["snapshot"]["element"][1:3])
    new["differential"]["element"][0]["short"] = "Unselected guidance"
    malformed = profile("Malformed")
    malformed["snapshot"]["element"].append(malformed["snapshot"]["element"][0])
    missing = profile("MissingSnapshot")
    del missing["snapshot"]
    renamed = profile("Renamed", url="https://example.org/new-canonical")
    dup1 = profile("Duplicate1", url="https://example.org/duplicate")
    dup2 = profile("Duplicate2", url="https://example.org/duplicate")
    vs = {"resourceType": "ValueSet", "id": "VS", "url": "https://example.org/vs"}
    archive(cache, left, [old, malformed, missing, dup1, dup2, vs])
    archive(cache, right, [new, renamed, vs], deps={"example": "1.0.0", "excluded": "5.0.0"})
    archive(cache, "excluded#5.0.0", [], release="5.0.0")
    config = project_config(tmp_path, [left, right])
    index.sync(config)
    monkeypatch.setattr(
        validator, "health", lambda *a: pytest.fail("Discovery must not call service")
    )
    discovered = packages.contexts(config, limit=1)
    info = discovered.data
    identity = discovered.dataset_id
    assert info["total"] == 3 and info["next_offset"] == 1
    assert info["default_package"] == left and info["default_package_source"] == "configuration"
    assert info["packages"][0]["fhir_versions"] == ["4.0.1"]
    assert packages.contexts(config, package="excluded#5.0.0").data["packages"][0][
        "excluded_reason"
    ]
    assert packages.contexts(config, package="absent#1.0.0").status == "not_found"
    assert packages.contexts(config, offset=1, dataset_id=identity).data["total"] == 3
    args = dict(left_package=left, right_package=right, config_path=config)
    owned = comparison.compare(mode="package", **args, limit=100).data["items"]
    assert (
        next(i for i in owned if i.get("identity", [None])[-1] == "https://example.org/duplicate")[
            "kind"
        ]
        == "uncomparable"
    )
    unavailable = next(i for i in owned if i.get("identity", [None])[-1] == malformed["url"])
    assert not unavailable["before"][0]["profile_details_available"]
    with pytest.raises(Error, match="ownership"):
        comparison.compare("Malformed", mode="references", **args)
    assert (
        comparison.compare(
            "Malformed", mode="references", left_package=left, right_package=left
        ).status
        == "effective_definition_unavailable"
    )
    compared = comparison.compare("PatientProfile", limit=100, **args)
    result = compared.data
    assert compared.dataset_id == identity
    assert result["resource_changed"] and result["unselected_representation_changed"]
    changes = result["changes"]
    assert result["total"] == len(changes) == 7
    assert any(c["field"] == "element_order" for c in changes)
    minimum = next(c for c in changes if c["field"] == "min")
    assert minimum["element_id"] == "Patient.identifier"
    assert minimum["before"]["value"] == 1 and minimum["after"]["value"] == 0
    assert next(c for c in changes if c["field"] == "description")["after"]["value_truncated"]
    # Every direct value is the cited source JSON, including absence versus explicit null.
    for change in changes:
        if change["field"] == "element_order":
            continue
        for side, resource in (("before", old), ("after", new)):
            evidence = change[side]
            if not evidence["present"]:
                continue
            value = pointer_value(resource, evidence["source"]["pointer"])
            if evidence.get("value_truncated"):
                assert evidence["value_sha256"] == digest(value)
            else:
                assert evidence["value"] == value
    reverse = comparison.compare("PatientProfile", left_package=right, right_package=left).data
    assert len(reverse["changes"]) == len(changes)
    for a, b in zip(changes, reverse["changes"], strict=True):
        assert a["before"] == b["after"] and a["after"] == b["before"]
        assert {"added": "removed", "removed": "added", "changed": "changed"}[a["kind"]] == b[
            "kind"
        ]
    pages = []
    offset = 0
    while offset is not None:
        page = comparison.compare(
            "PatientProfile", **args, limit=2, offset=offset, dataset_id=identity
        ).data
        pages.extend(page["changes"])
        offset = page["next_offset"]
    assert pages == changes
    assert (
        comparison.compare("PatientProfile", left_package=left, right_package=left).data["total"]
        == 0
    )
    assert (
        comparison.compare("PatientProfile", **args, element="Patient.identifier").data["total"]
        == 1
    )
    assert (
        comparison.compare("PatientProfile", **args, element="Patient.unknown").status
        == "not_found"
    )
    assert comparison.compare("PatientProfile", **args, view="differential").data["total"] == 3
    for name in ("Malformed", "MissingSnapshot"):
        assert comparison.compare(name, **args).status == "effective_definition_unavailable"
    assert comparison.compare("https://example.org/duplicate", **args).status == "ambiguous"
    assert comparison.compare("Renamed", **args).status == "not_found"
    assert comparison.compare("PatientProfile", **args, right_selector="Renamed").status == "ok"
    assert (
        comparison.compare("PatientProfile", **args, left_artifact_version="2.0.0").status
        == "not_found"
    )
    with pytest.raises(Error, match="Selector"):
        comparison.compare(None, **args)
    for kwargs in ({"view": "raw"}, {"limit": 0}, {"offset": 1}, {"limit": True}, {"offset": -1}):
        with pytest.raises(Error):
            comparison.compare("PatientProfile", **args, **kwargs)
    with pytest.raises(Error, match="StructureDefinition"):
        comparison.compare("VS", **args)
    # A pending lock/config must not replace published discovery facts or comparison identity.
    lock = config.with_name("specfhir.lock")
    pending = json.loads(lock.read_text())
    pending["roots"].append("pending#1.0.0")
    lock.write_text(json.dumps(pending))
    pending_info = packages.contexts(config).data
    assert not pending_info["index_matches_lock"] and not pending_info["config_matches_lock"]
    assert (
        packages.contexts(config).dataset_id == identity
        and "pending#1.0.0" not in pending_info["published_roots"]
    )
    config.write_text(
        config.read_text().replace(f'default_package="{left}"', f'default_package="{right}"')
    )
    changed_default = packages.contexts(config).data
    assert changed_default["default_package"] == right
    assert packages.contexts(config).dataset_id == identity
    assert comparison.compare("PatientProfile", **args).data["left"]["source"]["package"] == left
    cli = CliRunner().invoke(
        app,
        [
            "compare",
            "PatientProfile",
            "--left-package",
            left,
            "--right-package",
            right,
            "--config",
            str(config),
            "--json",
        ],
    )
    assert (
        cli.exit_code == 0
        and json.loads(cli.stdout)["data"] == comparison.compare("PatientProfile", **args).data
    )
    with db.connect() as conn:
        # If an owned artifact disappears, its dependency must not silently replace it.
        conn.execute(
            "DELETE FROM elements WHERE artifact_id IN "
            "(SELECT id FROM artifacts WHERE package_key=%s AND resource_id='PatientProfile')",
            (right,),
        )
        conn.execute(
            "UPDATE artifacts SET canonical='https://example.org/renamed',"
            "name='RenamedAgain',resource_id='RenamedAgain' "
            "WHERE package_key=%s AND resource_id='PatientProfile'",
            (right,),
        )
    with pytest.raises(Error, match="ownership"):
        comparison.compare("PatientProfile", **args)
    with db.connect() as conn:
        conn.execute("UPDATE index_state SET identity=%s", ("f" * 64,))
    for operation in (
        lambda: comparison.compare("PatientProfile", **args, offset=2, dataset_id=identity),
        lambda: packages.contexts(config, offset=1, dataset_id=identity),
        lambda: search.inspect("PatientProfile", package=left, dataset_id=identity),
    ):
        with pytest.raises(Error, match="dataset changed"):
            operation()


def test_order_and_other_fields():
    before = profile()
    after = copy.deepcopy(before)
    after["snapshot"]["element"].insert(1, {"id": "Patient.active", "path": "Patient.active"})
    after["snapshot"]["element"][2]["constraint"] = [{"key": "a"}, {"key": "b"}]
    before["snapshot"]["element"][1]["constraint"] = [{"key": "b"}, {"key": "a"}]
    before["snapshot"]["element"][1]["mapping"] = [{"identity": "x"}]
    after["snapshot"]["element"][2]["unknown/~"] = False

    def row(resource):
        return dict(
            resource=resource,
            package_key="example#1.0.0",
            resource_type="StructureDefinition",
            resource_id="PatientProfile",
            canonical=resource["url"],
            version="1.0.0",
            file_path="package/profile.json",
        )

    changes = list(comparison.differences(row(before), row(after), "snapshot"))
    assert {c["field"] for c in changes} == {None, "constraint", "mapping", "unknown/~"}
    assert next(c for c in changes if c["field"] == "unknown/~")["after"]["source"][
        "pointer"
    ].endswith("unknown~1~0")
    assert (
        pointer_value(
            after,
            next(c for c in changes if c["field"] == "unknown/~")["after"]["source"]["pointer"],
        )
        is False
    )


def test_package_and_reference_comparison(tmp_path, database):
    left, right = "example#1.0.0", "example#2.0.0"
    cache = tmp_path / ".specfhir/packages"
    old = profile()
    target = profile("Target")
    changed_target = copy.deepcopy(target)
    changed_target["snapshot"]["element"][0]["short"] = "Changed dependency guidance"
    old["baseDefinition"] = target["url"]
    elements = old["snapshot"]["element"]
    elements[0]["contentReference"] = "#" + elements[0]["id"]
    elements[1]["binding"] = {"valueSet": "https://example.org/excluded"}
    elements[2]["type"] = [
        {
            "code": "Reference",
            "profile": ["https://example.org/missing"],
            "targetProfile": ["https://example.org/ambiguous", "https://example.org/outside"],
        }
    ]
    removed = profile("Removed")
    duplicate = profile("Duplicate", url="https://example.org/ambiguous")
    duplicate2 = profile("Duplicate2", url=duplicate["url"])
    excluded = {"resourceType": "ValueSet", "id": "excluded", "url": "https://example.org/excluded"}
    vs = {
        "resourceType": "ValueSet",
        "id": "VS",
        "url": "https://example.org/vs",
        "compose": {"include": [{"valueSet": ["https://example.org/imported"]}]},
    }
    imported = {"resourceType": "ValueSet", "id": "imported", "url": "https://example.org/imported"}
    archive(
        cache, left, [old, removed, vs], deps={"dep": "1.0.0", "excluded": "5.0.0", "gone": "1.0.0"}
    )
    archive(
        cache,
        right,
        [old, profile("Added"), vs],
        deps={"dep": "2.0.0", "excluded": "5.0.0", "new": "1.0.0"},
    )
    archive(cache, "dep#1.0.0", [target, duplicate, duplicate2, imported])
    archive(
        cache,
        "dep#2.0.0",
        [changed_target, removed, duplicate, duplicate2, {**imported, "description": "New import"}],
    )
    archive(cache, "excluded#5.0.0", [excluded], release="5.0.0")
    archive(cache, "gone#1.0.0", [])
    archive(cache, "new#1.0.0", [])
    archive(cache, "outside#1.0.0", [profile("Outside", url="https://example.org/outside")])
    config = project_config(tmp_path, [left, right, "outside#1.0.0"])
    index.sync(config)
    args = dict(left_package=left, right_package=right, config_path=config)
    result = comparison.compare(mode="package", **args, limit=100)
    items = result.data["items"]
    artifacts = {item["identity"][2]: item for item in items if item["category"] == "artifact"}
    assert artifacts[removed["url"]]["kind"] == "removed"  # dependency must not replace it
    assert artifacts[old["url"]]["kind"] == "unchanged"
    pins = {item["name"]: item for item in items if item["category"] == "dependency_pin"}
    assert pins["dep"]["kind"] == "changed"
    assert pins["gone"]["kind"] == "removed" and pins["new"]["kind"] == "added"
    assert pins["excluded"]["before"][0]["excluded_reason"]
    paged = []
    offset = 0
    while offset is not None:
        page = comparison.compare(
            mode="package", **args, offset=offset, limit=2, dataset_id=result.dataset_id
        ).data
        paged.extend(page["items"])
        offset = page["next_offset"]
    assert paged == items
    refs = comparison.compare("PatientProfile", mode="references", **args, limit=100).data
    assert not refs["resource_changed"] and refs["max_depth"] == 1
    by_literal = {r["literal"]: r for r in refs["items"]}
    base = by_literal[target["url"]]
    assert (
        base["literal_unchanged"] and base["kind"] == "changed" and base["target_content_changed"]
    )
    assert base["before"]["target"]["content_sha256"] == digest(target)
    assert base["after"]["target"]["content_sha256"] == digest(changed_target)
    assert by_literal["#Patient"]["before"]["target"]["traversal"] == "cycle"
    for literal, status in [
        ("excluded", "excluded"),
        ("missing", "not_found_in_scope"),
        ("ambiguous", "ambiguous"),
        ("outside", "outside_scope"),
    ]:
        item = by_literal["https://example.org/" + literal]
        assert item["kind"] == "uncomparable" and item["before"]["target"]["status"] == status
    imported_result = comparison.compare("VS", mode="references", **args).data["items"][0]
    assert (
        imported_result["relationship"] == "compose.valueSet"
        and imported_result["target_content_changed"]
    )
    reverse = comparison.compare(
        "PatientProfile", mode="references", left_package=right, right_package=left, limit=100
    ).data
    for a, b in zip(refs["items"], reverse["items"], strict=True):
        assert a["before"] == b["after"] and a["after"] == b["before"]
    for mode, selector in (("package", None), ("references", "PatientProfile")):
        kwargs = dict(mode=mode, selector=selector, **args)
        cli = CliRunner().invoke(
            app,
            [
                "compare",
                *([selector] if selector else []),
                "--mode",
                mode,
                "--left-package",
                left,
                "--right-package",
                right,
                "--config",
                str(config),
                "--json",
            ],
        )
        assert cli.exit_code == 0, cli.output
        assert json.loads(cli.output) == comparison.compare(**kwargs).model_dump(exclude_none=True)
        with pytest.raises(Error, match="changed"):
            comparison.compare(**kwargs, dataset_id="stale")
    # Closure set semantics terminate cycles and expose their exact edges.
    with db.connect() as conn:
        conn.execute("INSERT INTO package_dependencies VALUES (%s,%s)", ("dep#2.0.0", right))
    cyclic = comparison.compare(mode="package", **args).data
    assert not cyclic["left"]["dependency_cycle"] and cyclic["right"]["dependency_cycle"]
    assert any(
        i["category"] == "dependency_edge"
        and i["after"] == {"package_key": "dep#2.0.0", "dependency_key": right}
        for i in cyclic["items"]
    )
    with pytest.raises(Error, match="does not accept"):
        comparison.compare("PatientProfile", mode="package", **args)
