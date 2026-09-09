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
