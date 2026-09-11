"""Read-only installed-system readiness and publication coverage."""

from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from specfhir import packages, search
from specfhir.config import lock_path, split_key
from specfhir.models import Error, Lock


def run(config_path: Path, package=None, with_validator=False):
    config_path = config_path.resolve()
    lock = Lock.model_validate_json(lock_path(config_path).read_bytes())
    pins = {p.key: p for p in lock.packages}
    if package:
        split_key(package)
        if package not in {p.key for p in lock.packages}:
            raise Error(f"Package is not locked: {package}")
    rows = []

    def record(name, failures, evidence=None, skipped=False):
        rows.append(
            {
                "name": name,
                "status": "skipped" if skipped else "failed" if failures else "passed",
                "details": failures,
                "evidence": evidence,
            }
        )

    inventory = packages.inventory(config_path)
    record(
        "readiness",
        [
            key
            for key in ("index_matches_lock", "index_matches_runtime", "config_matches_lock")
            if not inventory[key]
        ],
        inventory,
    )
    health = inventory["validator"]
    record(
        "validator readiness",
        []
        if health.get("ready") and health.get("matches_lock")
        else ["Validator is unavailable or does not match the lock"],
        health,
        skipped=not with_validator,
    )
    publications = {p.package: p for p in lock.publications}
    page_packages = sorted({d.package for d in lock.documents} | set(publications))
    for page_package in page_packages:
        if package and page_package != package:
            continue
        name = f"publication:{page_package}"
        try:
            publication = publications.get(page_package)
            pinned = [d for d in lock.documents if d.package == page_package]
            failures = []
            if publication:
                preview = packages.pages(page_package, config_path)
                selected = {
                    urljoin(publication.url, publication.page_prefix + p["path"])
                    for p in preview["pages"]
                    if p["selected"]
                }
                if selected != {d.url for d in pinned if d.publication == publication.url}:
                    failures.append("Page selection differs")
            for page in pinned:
                result = search.inspect(
                    page.url, package=page.package, view="raw", config_path=config_path
                )
                data = result.data or {}
                resource = data.get("resource", {})
                source = data.get("source", {})
                if (
                    result.status != "ok"
                    or source.get("package") != page.package
                    or source.get("artifact_version") != page.package.split("#")[1]
                    or resource.get("source_sha256") != page.sha256
                    or resource.get("publication") != page.publication
                    or resource.get("publication_member") != page.member
                    or resource.get("selected_anchors", []) != page.anchors
                ):
                    failures.append(f"Page provenance differs: {page.url}")
            siblings = {
                d.package: d
                for d in lock.documents
                if d.package != page_package
                and d.package.split("#")[0] == page_package.split("#")[0]
            }
            scope = packages.dependency_closure(pins, page_package)
            for sibling in siblings.values():
                result = search.resolve(sibling.url, package=page_package, config_path=config_path)
                if sibling.package in scope:
                    if (
                        result.status != "ok"
                        or (result.data or {}).get("source", {}).get("package") != sibling.package
                    ):
                        failures.append(
                            f"Dependency release page provenance differs: {sibling.package}"
                        )
                elif result.status != "not_found":
                    failures.append(f"Sibling release page leaked into scope: {sibling.package}")
            record(
                name,
                failures,
                {"pages": len(pinned), "sha256": publication.sha256 if publication else None},
            )
        except (ValueError, OSError) as exc:
            record(name, [str(exc)])
    if not any(r["name"].startswith("publication:") for r in rows):
        record("publications", ["No pinned publications for selected scope"], skipped=True)
    counts = dict(Counter(r["status"] for r in rows))
    return {"status": "error" if counts.get("failed") else "ok", "counts": counts, "checks": rows}
