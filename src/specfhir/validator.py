"""Delegate instance validation to a checksum-pinned HL7 service; no FHIR rules here."""

import json
import tarfile
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import psycopg

from specfhir import db, search
from specfhir.config import digest, load, lock_path
from specfhir.files import checksum
from specfhir.models import Error, Lock, Result
from specfhir.packages import (
    archive_files,
    compatibility,
    dependencies,
    effective_dependencies,
    manifest,
    obtain,
)

VERSION = "6.10.4"
JAR_SHA256 = "1106b9d58f9e363e47bea7c4fc065841e5fc91fe9d062775c3bfdd212bd653cc"
MAX_INPUT = 10 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024


def support_lock() -> Lock:
    return Lock.model_validate_json(
        Path(__file__).with_name("validator-packages.json").read_bytes()
    )


def snapshot_identity(lock: Lock, default_package: str) -> str:
    return digest(
        {
            "protocol": 1,
            "lock": lock.model_dump(),
            "default_package": default_package,
            "support": support_lock().model_dump(),
            "validator_sha256": JAR_SHA256,
        }
    )


def setup(config_path: Path = Path("specfhir.toml")) -> dict:
    """Prepare an immutable package snapshot; Compose owns the pinned Java runtime."""
    config = load(config_path)
    lock = Lock.model_validate_json(lock_path(config_path).read_bytes())
    if lock.roots != sorted(config.packages):
        raise Error("Lock/config mismatch; run sync")
    work = config_path.resolve().parent / ".specfhir"
    pins = {p.key: p for p in lock.packages}
    for pin in support_lock().packages:
        if pin.key in pins and pins[pin.key].sha256 != pin.sha256:
            raise Error("Validator support package conflicts with retrieval checksum")
        obtain(work / "packages", pin.key, pin.url, pin.sha256)
        pins[pin.key] = pin
    identity = snapshot_identity(lock, config.default_package)
    directory = work / "validator-service"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / identity
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        root = Path(temporary)
        files = {}
        compatible = set()
        for pin in pins.values():
            archive = work / "packages" / f"{pin.key}.tgz"
            if not archive.is_file() or checksum(archive) != pin.sha256:
                raise Error(f"Missing or changed package {pin.key}; run sync / validator-setup")
            info = manifest(archive, pin.key)
            if dependencies(info) != pin.dependencies:
                raise Error(f"Package dependency mismatch: {pin.key}")
            if not compatibility(info):
                compatible.add(pin.key)
            for name, content in archive_files(archive):
                relative = Path(".fhir/packages") / pin.key / name
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o644)
                files[str(relative)] = checksum(target)
        contexts = {}
        for pin in lock.packages:
            if pin.key not in compatible:
                continue
            required = {p.key for p in support_lock().packages}
            pending = [pin.key]
            visited = set()
            while pending:
                key = pending.pop()
                if key in visited:
                    continue
                visited.add(key)
                required.add(key)
                pending.extend(effective_dependencies(pins[key]))
            contexts[pin.key] = sorted(k for k in required if not k.startswith("hl7.fhir.r5.core#"))
        content = {
            "snapshot_id": identity,
            "validator_sha256": JAR_SHA256,
            "index_lock_digest": digest(lock.model_dump()),
            "contexts": contexts,
            "default_package": config.default_package,
            "files": files,
        }
        (root / "manifest.json").write_text(json.dumps(content, sort_keys=True))
        root.chmod(0o755)
        for folder in root.rglob("*"):
            if folder.is_dir():
                folder.chmod(0o755)
        (root / "manifest.json").chmod(0o644)
        if destination.exists():
            # Existing snapshots are immutable: corruption needs explicit removal and recreation.
            if (destination / "manifest.json").read_text() != (root / "manifest.json").read_text():
                raise Error("Existing validator snapshot differs; remove it and rerun setup")
            if any(
                not (destination / n).is_file() or checksum(destination / n) != h
                for n, h in files.items()
            ):
                raise Error("Existing validator snapshot is corrupt; remove it and rerun setup")
        else:
            root.rename(destination)
        destination.chmod(0o755)
        pointer = directory / "current.new"
        pointer.write_text(identity + "\n")
        pointer.replace(directory / "current")
    return {
        "status": "ok",
        "validator_version": VERSION,
        "sha256": JAR_SHA256,
        "snapshot_id": identity,
        "next_step": "docker compose up -d --build --force-recreate --wait validator",
    }


def validate(
    instance: dict[str, Any],
    *,
    package: str | None = None,
    profile: str | None = None,
    terminology_mode: str = "offline",
    config_path: Path = Path("specfhir.toml"),
) -> Result:
    config_path = config_path.resolve()
    config = load(config_path)
    context = package or config.default_package
    data: dict[str, Any] = {
        "execution": "failed",
        "coverage": "unknown",
        "validator_version": VERSION,
        "validator_sha256": JAR_SHA256,
        "terminology_mode": terminology_mode,
        "findings": None,
    }
    try:
        if not isinstance(instance, dict) or not isinstance(instance.get("resourceType"), str):
            raise Error("Instance must be a FHIR JSON object with resourceType")
        encoded = json.dumps(instance, allow_nan=False).encode()
        if len(encoded) > MAX_INPUT:
            raise Error("Instance exceeds 10 MiB limit")
        if terminology_mode not in {"offline", "online"}:
            raise Error("terminology_mode must be offline or online")
        endpoint = config.validator.terminology_endpoint
        if terminology_mode == "online" and not endpoint:
            raise Error("Online terminology requires validator.terminology_endpoint in config")
        lock = Lock.model_validate_json(lock_path(config_path).read_bytes())
        if lock.roots != sorted(config.packages):
            raise Error("Lock/config mismatch; run sync")
        with db.connect() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            state = conn.execute("SELECT metadata FROM index_state").fetchone()
            if not state or state["metadata"].get("lock_digest") != digest(lock.model_dump()):
                raise Error("Published index differs from lock; run sync")
            rows = conn.execute(
                f"""{db.SCOPE}
                SELECT p.key,p.excluded_reason FROM packages p JOIN scope s USING(key)""",
                {"package": context},
            ).fetchall()
        rows.sort(key=lambda row: row["key"])
        selected = next((r for r in rows if r["key"] == context), None)
        if not selected or selected["excluded_reason"]:
            raise Error("Selected package is unavailable in the retrieval index")
        keys = {r["key"] for r in rows}
        pins = {p.key: p for p in lock.packages if p.key in keys}
        if set(pins) != keys:
            raise Error("Package dependency missing from lock")
        data.update(
            index_lock_digest=digest(lock.model_dump()),
            packages=sorted(keys),
            retrieval_exclusions=[r for r in rows if r["excluded_reason"]],
            dependency_resolutions=[
                {
                    "package": p.key,
                    "declared": declared,
                    "selected": selected,
                    "reason": "Explicit R4 core selection",
                }
                for p in pins.values()
                for declared, selected in p.dependency_resolutions.items()
            ],
            requested_profile=profile,
            validation_scope="explicit_profile" if profile else "base_R4_with_declared_profiles",
        )
        declared = set()
        references = set()

        def declarations(value):
            if isinstance(value, dict):
                if isinstance(value.get("reference"), str):
                    references.add(value["reference"])
                meta = value.get("meta")
                if isinstance(meta, dict):
                    profiles = meta.get("profile", [])
                    if not isinstance(profiles, list) or any(
                        not isinstance(p, str) for p in profiles
                    ):
                        raise Error("meta.profile must be a list of canonical strings")
                    declared.update(profiles)
                for child in value.values():
                    declarations(child)
            elif isinstance(value, list):
                for child in value:
                    declarations(child)

        declarations(instance)
        data["declared_profiles"] = sorted(declared)
        data["references"] = sorted(references)
        resolved = []
        for selector in dict.fromkeys(
            ([profile] if profile is not None else []) + sorted(declared)
        ):
            result = search.resolve(selector, package=context, config_path=config_path)
            if result.status != "ok" or not result.data:
                data["unresolved_profile"] = selector
                raise Error(f"Profile could not be resolved in package context: {selector}")
            source = result.data["source"]
            if (
                source["resource_type"] != "StructureDefinition"
                or not source["canonical"]
                or "artifact" not in result.data
            ):
                raise Error("Profile must resolve to a StructureDefinition")
            canonical = source["canonical"]
            if source.get("artifact_version"):
                canonical += "|" + source["artifact_version"]
            resolved.append(canonical)
        data["resolved_profiles"] = resolved
        support = support_lock()
        data["support_packages"] = [p.key for p in support.packages]
        for pin in support.packages:
            if pin.key in pins and pins[pin.key].sha256 != pin.sha256:
                raise Error("Validator support package conflicts with retrieval checksum")
            pins[pin.key] = pin
        service_url = config.validator.service_url
        if terminology_mode == "online":
            service_url = config.validator.online_service_url
            if not service_url:
                raise Error("Online validator service is not configured")
        identity = snapshot_identity(lock, config.default_package)
        payload = {
            "snapshot_id": identity,
            "package": context,
            "instance": encoded.decode(),
            "profile": resolved[0] if profile is not None else None,
            "terminology_mode": terminology_mode,
            "timeout_seconds": config.validator.timeout_seconds,
        }
        with httpx.stream(
            "POST",
            service_url.rstrip("/") + "/validate",
            json=payload,
            timeout=config.validator.timeout_seconds,
            trust_env=False,
        ) as response:
            if response.status_code != 200:
                messages = {
                    409: "Stale validator; run validator-setup and recreate service",
                    429: "Validator is busy; retry explicitly",
                    404: "Package context is not provisioned in the validator service",
                }
                raise Error(
                    messages.get(
                        response.status_code,
                        f"Validator service failed (HTTP {response.status_code})",
                    )
                )
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_OUTPUT:
                    raise Error("Validator output exceeds limit")
        reply = json.loads(content)
        if not isinstance(reply, dict):
            raise Error("Validator response must be a JSON object")
        if (
            reply.get("snapshot_id") != identity
            or reply.get("validator_sha256") != JAR_SHA256
            or reply.get("package") != context
            or reply.get("terminology_mode") != terminology_mode
            or reply.get("terminology_endpoint")
            != (endpoint if terminology_mode == "online" else "")
        ):
            raise Error("Validator service identity differs from the requested context")
        loaded = set(reply.get("loaded_packages", []))
        data["loaded_packages"] = sorted(loaded)
        required = {k for k in pins if not k.startswith("hl7.fhir.r5.core#")}
        if loaded != required:
            raise Error("Validator loaded packages differ from the pinned context")
        outcome = reply["outcome"]
        issues = outcome.get("issue")
        if outcome.get("resourceType") != "OperationOutcome" or not isinstance(issues, list):
            raise Error("Validator returned an invalid OperationOutcome")
        for issue in issues:
            if (
                not isinstance(issue, dict)
                or issue.get("severity")
                not in {"fatal", "error", "warning", "information", "success"}
                or not isinstance(issue.get("code"), str)
            ):
                raise Error("Validator returned an invalid issue")
        counts = Counter(i["severity"] for i in issues)
        errors = counts["error"] + counts["fatal"]
        fields = {
            "severity",
            "code",
            "details",
            "diagnostics",
            "location",
            "expression",
            "extension",
        }
        data.update(
            execution="completed",
            coverage="limited",
            terminology_endpoint=endpoint if terminology_mode == "online" else None,
            coverage_notes=[
                "Offline terminology is limited; no remote membership checks."
                if terminology_mode == "offline"
                else "Terminology coverage depends on the configured server.",
                "External references are not supplied; see validator issues.",
                "HL7 also checks declared profiles; zero errors do not guarantee conformance.",
            ],
            findings={"errors": errors, "warnings": counts["warning"], "counts": dict(counts)},
            issues=[{k: v for k, v in i.items() if k in fields} for i in issues[:500]],
            issues_truncated=len(issues) > 500,
            issue_count=len(issues),
        )
        return Result(status="ok", context=context, data=data)
    except httpx.HTTPError:
        return Result(
            status="error",
            context=context,
            data=data,
            message="Validator service unavailable or timed out; check docker compose ps/logs",
        )
    except (ValueError, OSError, KeyError, TypeError, psycopg.Error, tarfile.TarError) as exc:
        return Result(status="error", context=context, data=data, message=str(exc))


def health(service_url: str) -> dict:
    """Fetch service health; callers decide whether its identity is acceptable."""
    try:
        response = httpx.get(f"{service_url.rstrip('/')}/health", timeout=5, trust_env=False)
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict):
            return result
    except (httpx.HTTPError, ValueError):
        pass
    return {"ready": False}


def refresh(config_path: Path) -> dict:
    """Reuse a matching warm service; explicitly recreate it when its snapshot changes."""
    import subprocess

    config_path = config_path.resolve()
    project = config_path.parent
    if not (project / "compose.yaml").is_file():
        raise Error("Coordinated refresh requires compose.yaml beside the configuration")
    config = load(config_path)
    lock = Lock.model_validate_json(lock_path(config_path).read_bytes())
    expected = snapshot_identity(lock, config.default_package)
    status = health(config.validator.service_url)
    if (
        status.get("ready") is True
        and status.get("snapshot_id") == expected
        and status.get("mode") == "offline"
    ):
        return {"status": "unchanged", "snapshot_id": expected, "ready": True}
    prepared = setup(config_path)
    try:
        subprocess.run(
            ["docker", "compose", "up", "-d", "--build", "--force-recreate", "--wait", "validator"],
            cwd=project,
            check=True,
            timeout=600,
            stdout=subprocess.DEVNULL,
        )
        status = health(config.validator.service_url)
        if (
            status.get("ready") is not True
            or status.get("snapshot_id") != prepared["snapshot_id"]
            or status.get("mode") != "offline"
        ):
            raise Error("Validator readiness snapshot differs from the prepared snapshot")
    except (subprocess.SubprocessError, httpx.HTTPError, OSError, ValueError) as exc:
        raise Error(
            "Index sync completed but validator refresh failed; rerun sync --with-validator"
        ) from exc
    return {**prepared, "ready": True}


def validate_cases(manifest_path: Path, config_path: Path) -> dict:
    """Run an explicit build manifest through the shared validator; retain every result."""
    from pydantic import BaseModel, ConfigDict, Field

    class Case(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(min_length=1)
        instance: str = Field(min_length=1)
        package: str = Field(min_length=1)
        profile: str = Field(min_length=1)

    class Cases(BaseModel):
        model_config = ConfigDict(extra="forbid")
        cases: list[Case] = Field(min_length=1, max_length=1000)

    cases = Cases.model_validate_json(manifest_path.read_bytes()).cases
    if len({c.name for c in cases}) != len(cases):
        raise Error("Build case names must be unique")
    results = []
    errors = warnings = failures = 0
    for case in cases:
        result: dict[str, Any]
        try:
            with (manifest_path.parent / case.instance).open("rb") as stream:
                raw = stream.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise Error("Instance exceeds 10 MiB limit")
            result = validate(
                json.loads(raw), package=case.package, profile=case.profile, config_path=config_path
            ).model_dump(exclude_none=True)
        except (ValueError, OSError) as exc:
            result = {"status": "error", "message": str(exc)}
        results.append({"name": case.name, "result": result})
        data = result.get("data", {})
        if data.get("execution") != "completed":
            failures += 1
        else:
            errors += data["findings"]["errors"]
            warnings += data["findings"]["warnings"]
    return {
        "status": "error" if failures else "ok",
        "data": {
            "execution": "failed" if failures else "completed",
            "findings": {"errors": errors, "warnings": warnings},
            "execution_failures": failures,
            "cases": results,
        },
    }
