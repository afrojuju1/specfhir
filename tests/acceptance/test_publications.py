import json
from pathlib import Path

import pytest

QUERIES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/retrieval/ig_queries.json").read_bytes()
)


@pytest.mark.parametrize("case", QUERIES, ids=lambda c: c["package"] + ":" + c["query"])
@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_workflow_retrieval(call, case, mode):
    result = call("search", **case, resource_type="Documentation", mode=mode)
    rows = result["data"]["results"]
    assert rows
    assert all(r["source"]["package"] == case["package"] for r in rows)
