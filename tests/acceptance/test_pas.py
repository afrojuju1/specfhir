import copy
import json

import pytest


@pytest.mark.parametrize("version", ["2.0.1", "2.1.0"])
def test_inquiry_versions(call, fhir, version):
    package = "hl7.fhir.us.davinci-pas#" + version
    other = "2.1.0" if version == "2.0.1" else "2.0.1"
    result = call("resolve", selector="PASClaimInquiry.identifier", package=package)["data"]
    assert result["element"]["min"] == (0 if version == "2.0.1" else 1)
    assert result["source"]["artifact_version"] == version
    assert result["source"]["package"] == package
    assert (
        call("resolve", selector=result["source"]["canonical"] + "|" + other, package=package)[
            "status"
        ]
        == "not_found"
    )
    result = call(
        "validate",
        instance=fhir("pas-inquiry-without-identifier"),
        profile="PASClaimInquiry",
        package=package,
    )["data"]
    assert result["execution"] == "completed"
    required = "Claim.identifier: minimum required = 1" in json.dumps(result["issues"])
    assert required == (version == "2.1.0")


@pytest.mark.parametrize("version", ["2.0.1", "2.1.0"])
@pytest.mark.parametrize(
    "label,profile,example",
    [
        ("request", "PASRequestBundle", "ReferralAuthorizationBundleExample"),
        ("response", "PASResponseBundle", "ReferralAuthorizationResponseBundleExample"),
    ],
)
def test_bundles(call, fhir, validate_published, version, label, profile, example):
    package = "hl7.fhir.us.davinci-pas#" + version
    other = "2.1.0" if version == "2.0.1" else "2.0.1"
    good = fhir(f"pas-{version}-{label}")
    result = call("validate", instance=good, package=package, profile=profile)["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    assert package in result["loaded_packages"]
    assert "hl7.fhir.us.davinci-pas#" + other not in result["loaded_packages"]
    assert result["dependency_resolutions"] == [
        {
            "package": "hl7.fhir.uv.subscriptions-backport.r4#1.1.0",
            "declared": "hl7.fhir.r4.core#4.0.0",
            "selected": "hl7.fhir.r4.core#4.0.1",
            "reason": "Explicit R4 core selection",
        }
    ]
    if label == "request":
        bad = copy.deepcopy(good)
        del bad["entry"][0]["resource"]["patient"]
        result = call("validate", instance=bad, package=package, profile=profile)["data"]
        assert result["findings"]["errors"] > 0
    validate_published(package, f"package/example/Bundle-{example}.json", profile)


def test_validation_context_matrix(call, fhir, record_property):
    from specfhir.config import digest

    packages = ["hl7.fhir.us.davinci-pas#2.0.1", "hl7.fhir.us.davinci-pas#2.1.0"]
    contexts = [{"package": p, "profile": "PASClaimInquiry"} for p in packages]
    instance = fhir("pas-inquiry-without-identifier")
    original = copy.deepcopy(instance)
    response = call("validate", instance=instance, contexts=contexts)
    data = response["data"]
    record_property("validation_matrix_outcome", json.dumps(response))
    assert instance == original and data["instance_sha256"] == digest(original)
    assert data["execution"] == "completed" and data["coverage"] == "per_context"
    assert not data["issues_identical"]
    correspondence = data["correspondence"]
    assert correspondence["status"] == "completed"
    for i, package in enumerate(packages):
        result = data["results"][i]["result"]
        outcome = result["data"]
        assert result["dataset_id"] == response["dataset_id"]
        assert outcome["coverage"] == "limited" and package in outcome["loaded_packages"]
        assert packages[1 - i] not in outcome["loaded_packages"]
        assert ("Claim.identifier: minimum required = 1" in json.dumps(outcome["issues"])) == (
            i == 1
        )
        assert sorted(
            n for group in correspondence["items"] for n in group["issue_indices"][i]
        ) == list(range(outcome["issue_count"]))
    minimum = next(
        g
        for g in correspondence["items"]
        if g.get("message_id") == "Validation_VAL_Profile_Minimum"
    )
    assert minimum["kind"] == "context_only" and minimum["issue_indices"][0] == []
    assert minimum["issue_indices"][1] and minimum["expression"] == ["Claim"]

    # Three real installed versions, using the same base R4 Patient and no profile rewrite.
    versions = ["3.1.1", "6.1.0", "7.0.0"]
    contexts = [{"package": "hl7.fhir.us.core#" + v} for v in versions]
    patient = {"resourceType": "Patient", "id": "synthetic"}
    stable = call(
        "validate", instance=patient, contexts=contexts, dataset_id=response["dataset_id"]
    )["data"]
    record_property("three_version_outcome", json.dumps(stable))
    assert stable["issues_identical"] and stable["findings"]["errors"] == 0
    assert [r["context"]["package"] for r in stable["results"]] == [c["package"] for c in contexts]
    assert stable["correspondence"]["available_contexts"] == [0, 1, 2]
    for i in range(3):
        result = stable["results"][i]["result"]
        assert contexts[i]["package"] in result["data"]["loaded_packages"]
        assert sorted(
            n for g in stable["correspondence"]["items"] for n in g["issue_indices"][i]
        ) == list(range(result["data"]["issue_count"]))
    failed = call(
        "validate",
        instance=patient,
        contexts=[contexts[0], {"package": "missing.example#0.0.0"}, contexts[2]],
        dataset_id=response["dataset_id"],
        expect_error=True,
    )["data"]
    assert failed["execution"] == "failed" and failed["findings"] is None
    assert failed["results"][1]["result"]["data"]["coverage"] == "unknown"
    assert failed["correspondence"]["status"] == "partial"
    assert failed["correspondence"]["available_contexts"] == [0, 2]
    assert all(g["issue_indices"][1] is None for g in failed["correspondence"]["items"])
