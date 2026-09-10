import json
import time

from specfhir.config import digest

LEFT = "hl7.fhir.us.davinci-pas#2.0.1"
RIGHT = "hl7.fhir.us.davinci-pas#2.1.0"
MEMBER = "package/StructureDefinition-profile-claim-inquiry.json"


def test_pas_comparison_workflow(call, published, record_property):
    discovered = call("contexts", limit=100)
    assert {LEFT, RIGHT} <= {p["key"] for p in discovered["data"]["packages"]}
    identity = discovered["dataset_id"]
    args = dict(
        selector="PASClaimInquiry", left_package=LEFT, right_package=RIGHT, dataset_id=identity
    )
    started = time.monotonic()
    forward = call("compare", **args, limit=10)["data"]
    record_property("comparison_seconds", round(time.monotonic() - started, 4))
    record_property("comparison_response_bytes", len(json.dumps(forward).encode()))
    assert forward["total"] > 10 and forward["next_offset"] == 10
    all_changes = forward["changes"][:]
    offset = forward["next_offset"]
    while offset is not None:
        page = call("compare", **args, offset=offset, limit=10)["data"]
        assert page["total"] == forward["total"]
        all_changes.extend(page["changes"])
        offset = page["next_offset"]
    assert len(all_changes) == forward["total"]
    # Expectations were reviewed from the original package JSON, independently of compare.
    minimum = next(
        c for c in all_changes if c["element_id"] == "Claim.identifier" and c["field"] == "min"
    )
    assert minimum["before"]["value"] == 0 and minimum["after"]["value"] == 1
    support = next(
        c for c in all_changes if c["element_id"] == "Claim.patient" and c["field"] == "mustSupport"
    )
    assert not support["before"]["present"] and support["after"]["value"] is True
    source_json = {LEFT: published(LEFT, MEMBER), RIGHT: published(RIGHT, MEMBER)}
    for change in all_changes:
        if change["field"] == "element_order":
            continue
        for side, package in (("before", LEFT), ("after", RIGHT)):
            evidence = change[side]
            assert evidence["source"]["package"] == package
            assert evidence["source"]["file"] == MEMBER
            if not evidence["present"]:
                continue
            value = source_json[package]
            for part in evidence["source"]["pointer"].split("/")[1:]:
                part = part.replace("~1", "/").replace("~0", "~")
                value = value[int(part)] if isinstance(value, list) else value[part]
            assert digest(value) == (
                evidence["value_sha256"]
                if evidence.get("value_truncated")
                else digest(evidence["value"])
            )
    inspected = call(
        "inspect",
        selector="PASClaimInquiry",
        element="Claim.identifier",
        package=RIGHT,
        dataset_id=identity,
    )
    assert inspected["dataset_id"] == identity
    assert inspected["data"]["element"]["min"] == 1
    reverse = call(
        "compare",
        selector="PASClaimInquiry",
        left_package=RIGHT,
        right_package=LEFT,
        element="Claim.identifier",
        dataset_id=identity,
    )["data"]
    reverse_min = next(c for c in reverse["changes"] if c["field"] == "min")
    assert reverse_min["before"] == minimum["after"] and reverse_min["after"] == minimum["before"]
    unchanged = call(
        "compare",
        selector="PASClaimInquiry",
        left_package=LEFT,
        right_package=LEFT,
        dataset_id=identity,
    )["data"]
    assert unchanged["total"] == 0 and not unchanged["resource_changed"]
    assert call("compare", **{**args, "selector": "no-such-profile"})["status"] == "not_found"
    authored = call("compare", **args, view="differential", limit=100)["data"]
    added = next(c for c in authored["changes"] if c["element_id"] == "Claim.identifier")
    assert added["kind"] == "added" and added["after"]["value"]["min"] == 1


def test_pas_package_and_target_workflow(call, published, record_property):
    args = dict(left_package=LEFT, right_package=RIGHT)
    started = time.monotonic()
    first = call("compare", mode="package", **args, limit=100)
    record_property("package_comparison_seconds", round(time.monotonic() - started, 4))
    record_property("package_comparison_response_bytes", len(json.dumps(first).encode()))
    identity = first["dataset_id"]
    items = first["data"]["items"][:]
    offset = first["data"]["next_offset"]
    while offset is not None:
        page = call(
            "compare", mode="package", **args, limit=100, offset=offset, dataset_id=identity
        )["data"]
        assert page["counts"] == first["data"]["counts"]
        items.extend(page["items"])
        offset = page["next_offset"]
    assert len(items) == first["data"]["total"] == 223
    assert first["data"]["counts"]["artifact"] == {"added": 17, "changed": 96, "removed": 11}
    # Every package JSON hash is independently checked against its locked archive.
    for item in items:
        if item["category"] != "artifact":
            continue
        for side in ("before", "after"):
            for evidence in item[side]:
                source = evidence["source"]
                if source["resource_type"] != "Documentation":
                    assert (
                        digest(published(source["package"], source["file"]))
                        == evidence["resource_sha256"]
                    )
    # Reviewed manifests: terminology changes and a direct HREX 1.1.0 pin is added.
    for package, version in ((LEFT, "5.3.0"), (RIGHT, "6.1.0")):
        assert (
            published(package, "package/package.json")["dependencies"]["hl7.terminology.r4"]
            == version
        )
    pins = {i["name"]: i for i in items if i["category"] == "dependency_pin"}
    assert pins["hl7.terminology.r4"]["kind"] == "changed"
    assert pins["hl7.fhir.us.davinci-hrex"]["kind"] == "changed"
    assert {r["key"] for r in pins["hl7.fhir.us.davinci-hrex"]["before"]} == {
        "hl7.fhir.us.davinci-hrex#1.0.0"
    }
    assert {r["key"] for r in pins["hl7.fhir.us.davinci-hrex"]["after"]} == {
        "hl7.fhir.us.davinci-hrex#1.0.0",
        "hl7.fhir.us.davinci-hrex#1.1.0",
    }
    assert (
        published("hl7.fhir.us.davinci-crd#2.0.0", "package/package.json")["dependencies"][
            "hl7.fhir.us.davinci-hrex"
        ]
        == "1.0.0"
    )
    assert any(
        i["category"] == "dependency_edge"
        and i["kind"] == "added"
        and i["after"] == {"package_key": RIGHT, "dependency_key": "hl7.fhir.us.davinci-hrex#1.1.0"}
        for i in items
    )
    # Claim Inquiry retains this literal base URL; the published target JSON differs.
    base_literal = "http://hl7.org/fhir/us/davinci-pas/StructureDefinition/profile-claim-base"
    for package in (LEFT, RIGHT):
        assert published(package, MEMBER)["baseDefinition"] == base_literal
    started = time.monotonic()
    targets = call(
        "compare",
        selector="PASClaimInquiry",
        mode="references",
        **args,
        dataset_id=identity,
        limit=100,
    )
    record_property("reference_comparison_seconds", round(time.monotonic() - started, 4))
    record_property("reference_comparison_response_bytes", len(json.dumps(targets).encode()))
    target_items = targets["data"]["items"][:]
    page = call(
        "compare",
        selector="PASClaimInquiry",
        mode="references",
        **args,
        dataset_id=identity,
        limit=100,
        offset=targets["data"]["next_offset"],
    )["data"]
    target_items.extend(page["items"])
    assert len(target_items) == targets["data"]["total"] == 120 and page["next_offset"] is None
    base = next(i for i in target_items if i["relationship"] == "baseDefinition")
    assert (
        base["literal"] == base_literal
        and base["literal_unchanged"]
        and base["target_content_changed"]
    )
    assert base["kind"] == "changed"
    for side, package in (("before", LEFT), ("after", RIGHT)):
        evidence = base[side]["target"]
        assert evidence["source"]["package"] == package
        assert evidence["content_sha256"] == digest(
            published(package, "package/StructureDefinition-profile-claim-base.json")
        )
    same = call(
        "compare", mode="package", left_package=LEFT, right_package=LEFT, dataset_id=identity
    )["data"]
    assert all(set(counts) == {"unchanged"} for counts in same["counts"].values())
