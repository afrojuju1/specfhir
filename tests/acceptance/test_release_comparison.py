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
