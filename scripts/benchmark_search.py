"""Compare reviewed source hits and warm latency; run after an embedding-enabled sync."""

import json
import platform
import statistics
import time
from pathlib import Path

from specfhir import db, index, search

root = Path(__file__).resolve().parents[1]
cases = json.loads((root / "tests/search_queries.json").read_text())
cases += json.loads((root / "tests/semantic_queries.json").read_text())
rows = []
for case in cases:
    for mode in ("lexical", "hybrid"):
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
        rank = next(
            (
                i
                for i, r in enumerate(result.data["results"], 1)
                if r["source"]["resource_id"] == case["resource_id"]
                and r["element_id"] == case["element_id"]
            ),
            None,
        )
        rows.append(
            {
                "query": case["query"],
                "mode": mode,
                "rank": rank,
                "warm_ms": round(statistics.median(durations), 1),
            }
        )
started = time.perf_counter()
assert index.sync(root / "specfhir.toml")["status"] == "unchanged"
unchanged_sync_seconds = round(time.perf_counter() - started, 3)
observation = root / ".specfhir/sync-observation.json"
observed = json.loads(observation.read_text()) if observation.exists() else {}
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
            "observed_sync": observed,
            "unchanged_sync_seconds": unchanged_sync_seconds,
            "counts": state["counts"],
            "preparation_seconds": state.get("preparation_seconds"),
            "database_bytes": int(size),
            "model_cache_bytes": sum(
                p.stat().st_size for p in (root / ".specfhir/models").rglob("*") if p.is_file()
            ),
            "results": rows,
        },
        indent=2,
    )
)
