"""Read-only acceptance for metadata-selected publication documentation."""

import json
from pathlib import Path
from urllib.parse import urljoin

from specfhir import packages, search
from specfhir.models import Lock

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "specfhir.toml"


def run():
    lock = Lock.model_validate_json((ROOT / "specfhir.lock").read_bytes())
    inventory = packages.inventory(CONFIG)
    assert inventory["index_matches_lock"] and inventory["config_matches_lock"]
    assert inventory["validator"]["matches_lock"] and inventory["validator"]["ready"]
    report = []
    for publication in lock.publications:
        preview = packages.pages(publication.package, CONFIG)
        selected = [p for p in preview["pages"] if p["selected"]]
        pinned = [d for d in lock.documents if d.publication == publication.url]
        assert {d.url for d in pinned} == {
            urljoin(publication.url, publication.page_prefix + p["path"]) for p in selected
        }
        for page in pinned:
            result = search.inspect(page.url, package=page.package, view="raw", config_path=CONFIG)
            assert result.status == "ok" and result.data, page.url
            assert result.data["source"]["package"] == publication.package
            assert result.data["source"]["artifact_version"] == publication.package.split("#")[1]
            artifact = result.data["resource"]
            assert artifact["source_sha256"] == page.sha256
            assert artifact["publication"] == publication.url
            assert artifact["publication_member"] == page.member
        for mode in ("lexical", "hybrid"):
            result = search.search(
                "privacy security",
                package=publication.package,
                resource_type="Documentation",
                mode=mode,
                config_path=CONFIG,
            )
            assert result.status == "ok" and result.data and result.data["results"]
            assert all(
                r["source"]["package"] == publication.package for r in result.data["results"]
            )
        # Sibling release documentation cannot resolve through this package context.
        sibling = next(
            (
                d
                for d in lock.documents
                if d.package != publication.package
                and d.package.split("#")[0] == publication.package.split("#")[0]
            ),
            None,
        )
        if sibling:
            assert (
                search.resolve(sibling.url, package=publication.package, config_path=CONFIG).status
                == "not_found"
            )
        report.append(
            {
                "package": publication.package,
                "publication_sha256": publication.sha256,
                "indexed_pages": len(pinned),
                "excluded_pages": len(preview["pages"]) - len(selected),
                "provenance_and_retrieval": "passed",
            }
        )
    assert report
    output = ROOT / ".specfhir/documentation-acceptance.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
