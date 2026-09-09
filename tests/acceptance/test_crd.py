import copy
import json

import pytest

VERSIONS = ["2.1.0", "2.2.1"]


@pytest.mark.parametrize(
    "kind,required", [("DeviceRequest", "status"), ("ServiceRequest", "authoredOn")]
)
@pytest.mark.parametrize("version", VERSIONS)
def test_orders(call, fhir, validate_published, kind, required, version):
    package = "hl7.fhir.us.davinci-crd#" + version
    other = next(v for v in VERSIONS if v != version)
    profile = "CRD" + kind
    result = call("resolve", selector=f"{profile}.{required}", package=package)["data"]
    assert result["element"]["min"] == 1
    assert result["source"]["package"] == package
    assert result["source"]["artifact_version"] == version
    assert (
        call("resolve", selector=result["source"]["canonical"] + "|" + other, package=package)[
            "status"
        ]
        == "not_found"
    )
    good = fhir("crd-" + kind)
    result = call("validate", instance=good, package=package, profile=profile)["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    assert package in result["loaded_packages"]
    assert not any("davinci-pas#" in p for p in result["loaded_packages"])
    bad = copy.deepcopy(good)
    del bad[required]
    result = call("validate", instance=bad, package=package, profile=profile)["data"]
    assert f"{kind}.{required}: minimum required = 1" in json.dumps(result["issues"])
    assert result["findings"]["errors"] > 0
    result = call("validate", instance=bad, package="hl7.fhir.r4.core#4.0.1")["data"]
    assert result["findings"]["errors"] == 0
    validate_published(package, f"package/example/{kind}-example.json", profile)
