"""Compare reviewed source hits and warm latency; run after an embedding-enabled sync."""

import argparse
import json
import platform
import statistics
import time
from pathlib import Path

from specfhir import db, embeddings, index, search

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--build",
    action="store_true",
    help="Measure uncached sample inference and a full cached rebuild instead of search",
)
args = parser.parse_args()
if args.build:
    with db.connect() as conn:
        pin = conn.execute("SELECT metadata FROM index_state").fetchone()["metadata"]["embedding"]
        sample = conn.execute(
            "SELECT heading,text FROM documents WHERE embedding IS NOT NULL "
            "ORDER BY text_hash,artifact_id,pointer,chunk LIMIT 512"
        ).fetchall()
    embeddings.load_model.cache_clear()
    started = time.perf_counter()
    model, tokenizer = embeddings.load_model(
        str(root / ".specfhir"), json.dumps(pin, sort_keys=True)
    )
    load_seconds = time.perf_counter() - started
    texts = [
        tokenizer.decode(tokenizer.encode(r["heading"], add_special_tokens=False).ids[:48])
        + "\n"
        + r["text"]
        for r in sample
    ]
    assert all(len(tokenizer.encode(t).ids) <= pin["max_tokens"] for t in texts)
    durations = []
    for _ in range(3):
        started = time.perf_counter()
        vectors = list(model.passage_embed(texts, batch_size=64))
        durations.append(time.perf_counter() - started)
        assert len(vectors) == len(texts)
        for value in vectors:
            embeddings.vector(value)
    rebuild = index.sync(root / "specfhir.toml", rebuild=True)
    unchanged = index.sync(root / "specfhir.toml")
    print(
        json.dumps(
            {
                "model_load_seconds": load_seconds,
                "sample_passages": len(texts),
                "uncached_inference_seconds": durations,
                "uncached_passages_per_second": len(texts) / statistics.median(durations),
                "rebuild": rebuild,
                "unchanged": unchanged,
            },
            indent=2,
        )
    )
    raise SystemExit
cases = json.loads((root / "tests/search_queries.json").read_text())
cases += json.loads((root / "tests/semantic_queries.json").read_text())
rows = []
for case in cases:
    for mode in ("lexical", "semantic", "hybrid"):
        kwargs = dict(
            package=case.get("package"),
            resource_type=case.get("resource_type"),
            mode=mode,
            config_path=root / "specfhir.toml",
        )
        search.search(case["query"], **kwargs)  # warm model and database pages
        durations = []
        for _ in range(3):
            started = time.perf_counter()
            result = search.search(case["query"], **kwargs)
            durations.append(1000 * (time.perf_counter() - started))

        def rank_of(results, expected):
            return next(
                (
                    i
                    for i, r in enumerate(results, 1)
                    if r["source"]["resource_id"] == expected["resource_id"]
                    and r["element_id"] == expected["element_id"]
                    and (
                        not expected.get("package") or r["source"]["package"] == expected["package"]
                    )
                ),
                None,
            )

        expanded = search.search(case["query"], limit=50, **kwargs)
        rows.append(
            {
                "query": case["query"],
                "mode": mode,
                "package": result.context,
                "expected": case,
                "rank": rank_of(result.data["results"], case),
                "rank_at_50": rank_of(expanded.data["results"], case),
                "top_results": result.data["results"],
                "warm_ms": round(statistics.median(durations), 1),
            }
        )
lookup_results = []
for case in json.loads((root / "tests/lookup_queries.json").read_text()):
    result = search.resolve(
        case["selector"], package=case["package"], config_path=root / "specfhir.toml"
    )
    assert result.status == case["status"], (case, result)
    if result.status == "ok":
        assert result.data["source"]["package"] == case["package"]
        assert result.data["source"]["artifact_version"] == case["package"].split("#")[1]
    if "min" in case:
        assert result.data["element"]["min"] == case["min"]
    lookup_results.append({"case": case, "result": result.model_dump(exclude_none=True)})
with db.connect() as conn:
    state = conn.execute("SELECT metadata FROM index_state").fetchone()["metadata"]
    size = conn.execute(
        "SELECT sum(pg_total_relation_size(relid)) AS bytes "
        "FROM pg_stat_user_tables WHERE schemaname=current_schema()"
    ).fetchone()["bytes"]
print(
    json.dumps(
        {
            "machine": platform.platform(),
            "counts": state["counts"],
            "database_bytes": int(size),
            "model_cache_bytes": sum(
                p.stat().st_size for p in (root / ".specfhir/models").rglob("*") if p.is_file()
            ),
            "results": rows,
            "lookup_results": lookup_results,
        },
        indent=2,
    )
)
