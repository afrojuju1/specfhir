import copy
import json

import pytest

PACKAGE = "hl7.fhir.us.davinci-dtr#2.2.0"
PROFILE = "DTRStdQuestionnaire"


def test_definitions(call):
    result = call("resolve", selector=PROFILE + ".subjectType", package=PACKAGE)
    assert result["data"]["element"]["min"] == 1
    source = result["data"]["source"]
    assert source["package"] == PACKAGE and source["artifact_version"] == "2.2.0"
    assert (
        call("resolve", selector=source["canonical"] + "|2.0.1", package=PACKAGE)["status"]
        == "not_found"
    )
    result = call("resolve", selector="QuestionnairePackage", package=PACKAGE)
    assert result["data"]["source"]["package"] == PACKAGE
    result = call("inspect", selector=PROFILE, package=PACKAGE)
    assert result["data"]["references"]["status"] == "completed"


def test_questionnaire(call, fhir):
    good = fhir("dtr-questionnaire")
    result = call("validate", instance=good, package=PACKAGE, profile=PROFILE)["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    assert {PACKAGE, "hl7.fhir.us.davinci-pas#2.2.1"} <= set(result["loaded_packages"])
    assert "hl7.fhir.us.davinci-pas#2.0.1" not in result["loaded_packages"]
    bad = copy.deepcopy(good)
    del bad["subjectType"]
    result = call("validate", instance=bad, package=PACKAGE, profile=PROFILE)["data"]
    assert result["findings"]["errors"] == 1
    assert "Questionnaire.subjectType: minimum required = 1" in json.dumps(result["issues"])
    result = call("validate", instance=bad, package="hl7.fhir.r4.core#4.0.1")["data"]
    assert result["findings"]["errors"] == 0


@pytest.mark.parametrize(
    "name",
    [
        "AdaptiveSearchExample",
        "dtr-questionnaire",
        "home-o2-std-questionnaire",
        "referred-questionnaire",
    ],
)
def test_published_questionnaire(call, published, name, record_property):
    instance = published(PACKAGE, f"package/example/Questionnaire-{name}.json")
    result = call("validate", instance=instance, package=PACKAGE)
    assert result["data"]["execution"] == "completed"
    record_property("published_outcome", json.dumps(result))
