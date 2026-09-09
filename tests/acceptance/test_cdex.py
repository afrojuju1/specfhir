import copy
import json

import pytest

VERSIONS = ["2.0.0", "2.1.0"]
PROFILE = "CDexTaskDataRequest"


@pytest.mark.parametrize("version", VERSIONS)
def test_task(call, fhir, published, version, record_property):
    package = "hl7.fhir.us.davinci-cdex#" + version
    other = next(v for v in VERSIONS if v != version)
    result = call("resolve", selector=PROFILE + ".authoredOn", package=package)["data"]
    assert result["element"]["min"] == 1
    assert result["source"]["package"] == package
    assert result["source"]["artifact_version"] == version
    assert (
        call("resolve", selector=result["source"]["canonical"] + "|" + other, package=package)[
            "status"
        ]
        == "not_found"
    )
    result = call("resolve", selector=PROFILE + ".meta.tag.system", package=package)
    assert result["data"]["element"]["min"] == (0 if version == "2.0.0" else 1)
    result = call(
        "resolve",
        selector="http://hl7.org/fhir/us/davinci-cdex/OperationDefinition/submit-attachment",
        package=package,
    )
    assert result["data"]["source"]["package"] == package
    result = call("inspect", selector=PROFILE, package=package)
    assert result["data"]["references"]["status"] == "completed"
    good = fhir("cdex-task")
    result = call("validate", instance=good, package=package, profile=PROFILE)["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    assert package in result["loaded_packages"]
    assert "hl7.fhir.us.davinci-cdex#" + other not in result["loaded_packages"]
    tagged = copy.deepcopy(good)
    tagged["meta"] = {"tag": [{"code": "synthetic-tag"}]}
    result = call("validate", instance=tagged, package=package, profile=PROFILE)["data"]
    if version == "2.0.0":
        assert result["findings"]["errors"] == 0
    else:
        assert "Task.meta.tag.system: minimum required = 1" in json.dumps(result["issues"])
        assert result["findings"]["errors"] > 0
    bad = copy.deepcopy(good)
    del bad["authoredOn"]
    result = call("validate", instance=bad, package=package, profile=PROFILE)["data"]
    assert result["findings"]["errors"] > 0
    assert "Task.authoredOn: minimum required = 1" in json.dumps(result["issues"])
    result = call(
        "validate",
        instance=bad,
        package=package,
        profile="http://hl7.org/fhir/StructureDefinition/Task",
    )["data"]
    assert result["findings"]["errors"] == 0
    for name, profile in [("1", PROFILE), ("19", "CDexTaskAttachmentRequest")]:
        instance = published(package, f"package/example/Task-cdex-task-example{name}.json")
        result = call("validate", instance=instance, package=package, profile=profile)
        assert result["data"]["execution"] == "completed"
        record_property("published_outcome_" + name, json.dumps(result))
