import json
from pathlib import Path

import pytest

from specfhir import packages

QUERIES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/retrieval/ig_queries.json").read_bytes()
)


@pytest.mark.parametrize("case", QUERIES, ids=lambda c: c["package"] + ":" + c["query"])
@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_workflow_retrieval(call, case, mode, locked):
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
        scope = packages.dependency_closure({p.key: p for p in locked.packages}, case["package"])
        assert all(r["source"]["package"] in scope for r in rows)
        assert all(r["relationship"] == "relevance_candidate" for r in rows)


def test_comparison_guidance(call):
    roots = ["hl7.fhir.us.davinci-pas#2.0.1", "hl7.fhir.us.davinci-pas#2.1.0"]
    comparison = call(
        "compare",
        selector="PASClaimInquiry",
        left_package=roots[0],
        right_package=roots[1],
        element="Claim.identifier",
    )
    dataset = comparison["dataset_id"]
    for package in roots:
        result = call(
            "search",
            query="authorization inquiry",
            package=package,
            resource_type="Documentation",
            mode="lexical",
            dataset_id=dataset,
        )
        hit = next(r for r in result["data"]["results"] if r["source"]["package"] == package)
        assert hit["relationship"] == "relevance_candidate"
        page = call(
            "inspect",
            selector=hit["source"]["canonical"],
            package=package,
            view="passages",
            pointer=hit["source"]["pointer"],
            limit=1,
            dataset_id=dataset,
        )
        data = page["data"]
        assert data["passages"] and data["source"]["package"] == package
        assert all(
            link["relationship"] == "published_link" for link in data["passages"][0]["links"]
        )
        if data["next_offset"] is not None:
            following = call(
                "inspect",
                selector=hit["source"]["canonical"],
                package=package,
                view="passages",
                pointer=hit["source"]["pointer"],
                limit=1,
                offset=data["next_offset"],
                dataset_id=dataset,
            )
            assert following["data"]["passages"][0]["chunk"] != data["passages"][0]["chunk"]
        # Core guidance is a locked dependency, not an inferred explanation of this change.
        core = call(
            "inspect",
            selector="https://hl7.org/fhir/R4/profiling.html#cardinality",
            package=package,
            view="passages",
            limit=1,
            dataset_id=dataset,
        )
        assert core["data"]["source"]["package"] == "hl7.fhir.r4.core#4.0.1"
        assert core["data"]["passages"][0]["anchor"] == "cardinality"
        assert core["data"]["passages"][0]["links"]
        missing = call(
            "inspect",
            selector="https://hl7.org/fhir/R4/profiling.html#not-published",
            package=package,
            view="passages",
            dataset_id=dataset,
        )
        assert missing["status"] == "not_found"
    call("search", query="cardinality", package=roots[0], dataset_id="stale", expect_error=True)

    linked = call(
        "inspect",
        selector="https://hl7.org/fhir/R4/profiling.html#conf-res",
        package=roots[0],
        view="passages",
        dataset_id=dataset,
    )
    target = next(
        link["url"]
        for link in linked["data"]["passages"][0]["links"]
        if link["url"].endswith("/extensibility.html")
    )
    followed = call(
        "inspect", selector=target, package=roots[0], view="passages", dataset_id=dataset
    )
    assert followed["data"]["source"]["canonical"] == target
    assert followed["data"]["source"]["package"] == "hl7.fhir.r4.core#4.0.1"
    omitted = call(
        "inspect",
        selector="https://hl7.org/fhir/R4/bundle.html#resource",
        package=roots[0],
        view="passages",
        dataset_id=dataset,
    )
    assert omitted["status"] == "not_found"  # Generated resource table deliberately excluded.
