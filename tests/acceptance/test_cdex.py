import copy
import json

import pytest

VERSIONS = ["2.0.0", "2.1.0"]
PROFILE = "CDexTaskDataRequest"


@pytest.mark.parametrize("version", VERSIONS)
def test_task(call, fhir, validate_published, published, version):
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
        "inspect",
        selector="http://hl7.org/fhir/us/davinci-cdex/OperationDefinition/submit-attachment",
        package=package,
    )
    assert result["data"]["source"]["package"] == package
    references = result["data"]["references"]
    relationships = {item["relationship"] for item in references["items"]}
    assert "operation.parameter.binding.valueSet" in relationships
    assert ("operation.inputProfile" in relationships) == (version == "2.1.0")
    external = next(
        item for item in references["items"] if item["target"].startswith("http://loinc.org")
    )
    assert external["status"] == "not_found_in_scope"
    operation = published(package, "package/OperationDefinition-submit-attachment.json")
    for item in references["items"]:
        value = operation
        for part in item["pointer"].split("/")[1:]:
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert value == item["target"]
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
        validate_published(package, f"package/example/Task-cdex-task-example{name}.json", profile)


def test_core_context_stays_pinned(call, fhir):
    # CDEX URLs used to auto-load the latest cached IG into this core-only engine.
    instance = fhir("cdex-task")
    for _ in range(2):
        result = call("validate", instance=instance, package="hl7.fhir.r4.core#4.0.1")["data"]
        assert result["execution"] == "completed"
        assert not any("davinci-" in p for p in result["loaded_packages"])
        assert result["findings"]["errors"] == 2
        assert "No definition could be found for URL value" in json.dumps(result["issues"])
    result = call(
        "validate", instance={"resourceType": "Patient"}, package="hl7.fhir.r4.core#4.0.1"
    )["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    assert not any("davinci-" in p for p in result["loaded_packages"])
