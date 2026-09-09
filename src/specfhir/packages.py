"""Bounded exact-version package acquisition; no dependency version substitution."""

import hashlib
import json
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from specfhir.config import Config, split_key
from specfhir.models import Error, Lock, PackagePin

REGISTRY = "https://packages.fhir.org"
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024


def archive_files(path: Path) -> Iterator[tuple[str, bytes]]:
    """Read bounded regular members without extracting anything to disk."""
    seen: set[str] = set()
    total = 0
    with tarfile.open(path, mode="r|gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name)
            if parts.is_absolute() or ".." in parts.parts or "\\" in member.name:
                raise Error(f"Unsafe archive path: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise Error(f"Unsupported archive member: {member.name}")
            name = str(parts)
            if name in seen:
                raise Error(f"Duplicate archive member: {name}")
            seen.add(name)
            total += member.size
            if member.size > MAX_FILE or total > MAX_EXPANDED or len(seen) > 100_000:
                raise Error(f"Archive limits exceeded: {path.name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise Error(f"Unreadable archive member: {name}")
            yield name, stream.read(MAX_FILE + 1)


def manifest(path: Path, key: str) -> dict[str, Any]:
    for name, data in archive_files(path):
        if name == "package/package.json":
            value = json.loads(data)
            if not isinstance(value, dict):
                raise Error(f"Manifest must be a JSON object: {key}")
            expected_name, expected_version = split_key(key)
            if value.get("name") != expected_name or value.get("version") != expected_version:
                raise Error(f"Manifest identity does not match {key}")
            return value
    raise Error(f"Missing package/package.json in {key}")


def dependencies(value: dict[str, Any]) -> list[str]:
    deps = value.get("dependencies", {})
    if not isinstance(deps, dict):
        raise Error("Package dependencies must be an object")
    keys = sorted(f"{name}#{version}" for name, version in deps.items())
    for key in keys:
        split_key(key)
    return keys


def compatibility(value: dict[str, Any]) -> str | None:
    versions = value.get("fhirVersions", value.get("fhir-version-list", []))
    if not isinstance(versions, list) or "4.0.1" not in versions:
        return f"FHIR versions {versions!r} do not declare R4 4.0.1 support"
    if str(value.get("type", "")).lower() in {"fhir.examples", "examples"}:
        return "Example package: instances are not knowledge definitions"
    return None


def obtain(cache: Path, key: str, url: str, checksum: str | None) -> tuple[Path, str]:
    split_key(key)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{key}.tgz"
    if not path.exists():
        if not url.startswith("https://"):
            raise Error("Package downloads require HTTPS")
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=90) as response:
                    response.raise_for_status()
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_ARCHIVE:
                            raise Error(f"Download too large: {key}")
                        stream.write(chunk)
                stream.flush()
                actual = hashlib.sha256(temporary.read_bytes()).hexdigest()
                if checksum and checksum != actual:
                    raise Error(f"Checksum mismatch for {key}")
                temporary.replace(path)
                path.with_suffix(".source-url").write_text(url)
            finally:
                temporary.unlink(missing_ok=True)
    if path.stat().st_size > MAX_ARCHIVE:
        raise Error(f"Archive too large: {key}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if checksum and checksum != actual:
        raise Error(f"Checksum mismatch for {key}; cache was not modified")
    return path, actual


def core_resolutions(key: str, declared: list[str]) -> dict[str, str]:
    # Approved upstream manifest exception; never a general version fallback.
    if (
        key == "hl7.fhir.uv.subscriptions-backport.r4#1.1.0"
        and "hl7.fhir.r4.core#4.0.0" in declared
    ):
        return {"hl7.fhir.r4.core#4.0.0": "hl7.fhir.r4.core#4.0.1"}
    return {}


def effective_dependencies(pin: PackagePin) -> list[str]:
    if pin.dependency_resolutions != core_resolutions(pin.key, pin.dependencies):
        raise Error(f"Unapproved dependency resolution in {pin.key}; run sync --update-lock")
    return sorted({pin.dependency_resolutions.get(dep, dep) for dep in pin.dependencies})


def resolve_lock(config: Config, cache: Path, previous: Lock | None) -> Lock:
    pins = {pin.key: pin for pin in previous.packages} if previous else {}
    if previous and (
        previous.roots != sorted(config.packages) or len(pins) != len(previous.packages)
    ):
        raise Error("Lock/config mismatch; run sync --update-lock")
    found: dict[str, PackagePin] = {}
    active: set[str] = set()

    def visit(key: str):
        if key in active:
            raise Error(f"Dependency cycle at {key}")
        if key in found:
            return
        if len(found) + len(active) >= 256:
            raise Error("Package graph exceeds 256 packages")
        if previous and key not in pins:
            raise Error(f"Dependency {key} missing from lock; run sync --update-lock")
        active.add(key)
        name, version = split_key(key)
        pin = pins.get(key)
        url = pin.url if pin else f"{REGISTRY}/{name}/{version}"
        origin = (cache / f"{key}.tgz").with_suffix(".source-url")
        if not pin and origin.is_file():
            recorded = origin.read_text().strip()
            if recorded not in (url, f"https://packages2.fhir.org/packages/{name}/{version}"):
                raise Error(f"Unrecognized cached package origin: {key}")
            url = recorded
        try:
            path, checksum = obtain(cache, key, url, pin.sha256 if pin else None)
        except httpx.HTTPStatusError as exc:
            if pin or exc.response.status_code != 404:
                raise
            url = f"https://packages2.fhir.org/packages/{name}/{version}"
            path, checksum = obtain(cache, key, url, None)
        value = manifest(path, key)
        deps = dependencies(value)
        if pin and deps != pin.dependencies:
            raise Error(f"Locked dependency edges do not match manifest: {key}")
        if key in config.packages and (reason := compatibility(value)):
            raise Error(f"Unsupported root {key}: {reason}")
        selected = PackagePin(
            key=key,
            url=url,
            sha256=checksum,
            dependencies=deps,
            dependency_resolutions=core_resolutions(key, deps),
        )
        if pin and pin.dependency_resolutions != selected.dependency_resolutions:
            raise Error(f"Dependency resolution policy changed for {key}; run sync --update-lock")
        for dep in effective_dependencies(selected):
            visit(dep)
        found[key] = selected
        active.remove(key)

    for key in sorted(config.packages):
        visit(key)
    if previous and set(found) != set(pins):
        raise Error("Lock contains unreachable packages; run sync --update-lock")
    return Lock(roots=sorted(config.packages), packages=[found[key] for key in sorted(found)])


def save_lock(path: Path, lock: Lock):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(lock.model_dump_json(indent=2) + "\n")
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def versions(name: str) -> dict:
    """Discover registry releases without changing configuration or the lock."""
    split_key(f"{name}#0.0.0")
    response = httpx.get(f"{REGISTRY}/{name}", timeout=30, follow_redirects=True)
    response.raise_for_status()
    metadata = response.json()
    releases = metadata.get("versions") if isinstance(metadata, dict) else None
    if not isinstance(releases, dict):
        raise Error("Registry response has no release metadata")
    return {
        "status": "ok",
        "package": name,
        "source": f"{REGISTRY}/{name}",
        "releases": [
            {"version": v, "fhir_version": info.get("fhirVersion"), "url": info.get("url")}
            for v, info in releases.items()
            if isinstance(info, dict)
        ],
    }


def inventory(config_path: Path) -> dict:
    """Report published coverage and whether the validator matches the current lock."""
    from specfhir import db, validator
    from specfhir.config import digest, load

    config = load(config_path)
    lock = Lock.model_validate_json(config_path.with_name("specfhir.lock").read_bytes())
    expected = validator.snapshot_identity(lock, config.default_package)
    with db.connect() as conn:
        state = conn.execute("SELECT metadata FROM index_state").fetchone()
    if not state:
        raise Error("Index unavailable; run sync")
    health = {"ready": False}
    try:
        response = httpx.get(f"{config.validator.service_url}/health", timeout=5, trust_env=False)
        response.raise_for_status()
        health = response.json()
        if not isinstance(health, dict):
            raise Error("Invalid validator health response")
    except (httpx.HTTPError, ValueError):
        health = {"ready": False}
    metadata = state["metadata"]
    return {
        "status": "ok",
        "configured_roots": config.packages,
        "published_roots": metadata["roots"],
        "index_matches_lock": metadata["lock_digest"] == digest(lock.model_dump()),
        "config_matches_lock": sorted(config.packages) == lock.roots
        and [d.model_dump() for d in config.documents]
        == [
            d.model_dump(exclude={"sha256", "publication", "member"})
            for d in lock.documents
            if d.publication is None
        ]
        and [p.model_dump() for p in config.publications]
        == [p.model_dump(exclude={"sha256"}) for p in lock.publications],
        "validator": {**health, "matches_lock": health.get("snapshot_id") == expected},
        "inventory": metadata["inventory"],
        "publications": [p.model_dump() for p in lock.publications],
        "counts": metadata["counts"],
        "reference_checks": metadata.get("reference_checks", {"status": "unavailable"}),
    }


def pages(key: str, config_path: Path) -> dict:
    """Preview metadata-selected publication pages without changing the lock or index."""
    from specfhir import documents
    from specfhir.config import load

    split_key(key)
    config_path = config_path.resolve()
    config = load(config_path)
    lock = Lock.model_validate_json(config_path.with_name("specfhir.lock").read_bytes())
    pin = next((p for p in lock.packages if p.key == key), None)
    if pin is None:
        raise Error(f"Package is not locked: {key}; run sync first")
    archive = config_path.parent / ".specfhir/packages" / f"{key}.tgz"
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin.sha256:
            raise Error(f"Package checksum changed: {key}")
    candidates = documents.page_candidates(archive, key)
    source = next((p for p in config.publications if p.package == key), None)
    return {
        "status": "ok",
        "package": key,
        "package_sha256": pin.sha256,
        "publication": source.model_dump() if source else None,
        "selected": sum(p["selected"] for p in candidates),
        "pages": candidates,
    }
