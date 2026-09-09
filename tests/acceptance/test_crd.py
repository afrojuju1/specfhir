import copy
import json

import pytest

PACKAGE = "hl7.fhir.us.davinci-crd#2.2.1"


@pytest.mark.parametrize(
    "kind,required", [("DeviceRequest", "status"), ("ServiceRequest", "authoredOn")]
)
def test_orders(call, fhir, published, kind, required, record_property):
    profile = "CRD" + kind
    result = call("resolve", selector=f"{profile}.{required}", package=PACKAGE)["data"]
    assert result["element"]["min"] == 1
    assert result["source"]["package"] == PACKAGE
    assert result["source"]["artifact_version"] == "2.2.1"
    assert (
        call("resolve", selector=result["source"]["canonical"] + "|2.1.0", package=PACKAGE)[
            "status"
        ]
        == "not_found"
    )
    good = fhir("crd-" + kind)
    result = call("validate", instance=good, package=PACKAGE, profile=profile)["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    assert PACKAGE in result["loaded_packages"]
    assert not any("davinci-pas#" in p for p in result["loaded_packages"])
    bad = copy.deepcopy(good)
    del bad[required]
    result = call("validate", instance=bad, package=PACKAGE, profile=profile)["data"]
    assert f"{kind}.{required}: minimum required = 1" in json.dumps(result["issues"])
    assert result["findings"]["errors"] > 0
    result = call("validate", instance=bad, package="hl7.fhir.r4.core#4.0.1")["data"]
    assert result["findings"]["errors"] == 0
    instance = published(PACKAGE, f"package/example/{kind}-example.json")
    result = call("validate", instance=instance, package=PACKAGE, profile=profile)
    assert result["data"]["execution"] == "completed"
    record_property("published_outcome", json.dumps(result))
