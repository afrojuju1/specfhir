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
MAX_EXPANDED = 1024 * 1024 * 1024


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
            finally:
                temporary.unlink(missing_ok=True)
    if path.stat().st_size > MAX_ARCHIVE:
        raise Error(f"Archive too large: {key}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if checksum and checksum != actual:
        raise Error(f"Checksum mismatch for {key}; cache was not modified")
    return path, actual


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
        path, checksum = obtain(cache, key, url, pin.sha256 if pin else None)
        value = manifest(path, key)
        deps = dependencies(value)
        if pin and deps != pin.dependencies:
            raise Error(f"Locked dependency edges do not match manifest: {key}")
        if key in config.packages and (reason := compatibility(value)):
            raise Error(f"Unsupported root {key}: {reason}")
        for dep in deps:
            visit(dep)
        found[key] = PackagePin(key=key, url=url, sha256=checksum, dependencies=deps)
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
