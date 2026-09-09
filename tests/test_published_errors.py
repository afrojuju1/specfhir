import copy

import pytest
from helpers import assert_published_errors


def test_reviewed_errors_detect_drift():
    issue = {
        "severity": "error",
        "extension": [
            {
                "url": "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id",
                "valueCode": "Reference_REF_CantResolve",
            }
        ],
    }
    data = {
        "execution": "completed",
        "issues_truncated": False,
        "findings": {"errors": 1},
        "issues": [issue, {"severity": "warning"}],
    }
    expected = {"Reference_REF_CantResolve": 1}
    assert_published_errors(data, expected)
    for mutation in ("added", "removed", "category", "fatal", "truncated", "failed"):
        changed = copy.deepcopy(data)
        if mutation == "added":
            changed["issues"].append(issue)
        elif mutation == "removed":
            changed["issues"].pop(0)
        elif mutation == "category":
            changed["issues"][0]["extension"][0]["valueCode"] = "Other"
        elif mutation == "fatal":
            changed["issues"][0]["severity"] = "fatal"
        elif mutation == "truncated":
            changed["issues_truncated"] = True
        else:
            changed["execution"] = "failed"
        with pytest.raises(AssertionError):
            assert_published_errors(changed, expected)
