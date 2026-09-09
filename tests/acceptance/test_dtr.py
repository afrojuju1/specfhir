import copy
import json

import pytest

PROFILE = "DTRStdQuestionnaire"


@pytest.fixture(scope="module", params=["2.1.0", "2.2.0"])
def package(request):
    return "hl7.fhir.us.davinci-dtr#" + request.param


def test_definitions(call, package):
    result = call("resolve", selector=PROFILE + ".subjectType", package=package)
    assert result["data"]["element"]["min"] == 1
    source = result["data"]["source"]
    assert source["package"] == package and source["artifact_version"] == package.split("#")[1]
    sibling = call(
        "resolve",
        selector=source["canonical"] + ("|2.2.0" if package.endswith("#2.1.0") else "|2.1.0"),
        package=package,
    )
    if package.endswith("#2.1.0"):
        assert sibling["status"] == "not_found"
    else:
        # 2.2.0 legitimately depends on 2.1.0 through PAS/CDEX.
        assert sibling["data"]["source"]["package"] == "hl7.fhir.us.davinci-dtr#2.1.0"
    result = call("resolve", selector="QuestionnairePackage", package=package)
    assert result["data"]["source"]["package"] == package
    result = call("inspect", selector=PROFILE, package=package)
    assert result["data"]["references"]["status"] == "completed"


def test_questionnaire(call, fhir, package):
    good = fhir("dtr-questionnaire")
    result = call("validate", instance=good, package=package, profile=PROFILE)["data"]
    assert result["execution"] == "completed" and result["findings"]["errors"] == 0
    pas = "2.1.0" if package.endswith("#2.1.0") else "2.2.1"
    assert {package, "hl7.fhir.us.davinci-pas#" + pas} <= set(result["loaded_packages"])
    assert "hl7.fhir.us.davinci-pas#2.0.1" not in result["loaded_packages"]
    bad = copy.deepcopy(good)
    del bad["subjectType"]
    result = call("validate", instance=bad, package=package, profile=PROFILE)["data"]
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
def test_published_questionnaire(call, published, name, record_property, package):
    if name == "dtr-questionnaire" and package.endswith("#2.1.0"):
        name = "dtr-base-questionnaire"
    instance = published(package, f"package/example/Questionnaire-{name}.json")
    result = call("validate", instance=instance, package=package)
    assert result["data"]["execution"] == "completed"
    record_property("published_outcome", json.dumps(result))
