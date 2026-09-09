import json
from pathlib import Path

import pytest

QUERIES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/retrieval/ig_queries.json").read_bytes()
)


@pytest.mark.parametrize("case", QUERIES, ids=lambda c: c["package"] + ":" + c["query"])
@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_workflow_retrieval(call, case, mode):
    result = call(
        "search",
        query=case["query"],
        package=case["package"],
        resource_type=case["resource_type"],
        limit=5,
        mode=mode,
    )
    rows = result["data"]["results"]
    assert any(
        r["source"]["canonical"] == case["expected_canonical"]
        and r["source"]["package"] == case["package"]
        for r in rows
    ), [(r["source"]["package"], r["source"]["canonical"]) for r in rows]
    if case["resource_type"] == "Documentation":
        assert all(r["source"]["package"] == case["package"] for r in rows)
