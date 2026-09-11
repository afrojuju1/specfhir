"""Shared synthetic package builders for isolated tests."""

import io
import json
import tarfile
from collections import Counter
from pathlib import Path


def assert_published_errors(data, expected):
    """Compare reviewed HL7 error categories, retaining warnings in the full outcome."""
    assert data["execution"] == "completed"
    assert not data["issues_truncated"]
    actual = Counter()
    for issue in data["issues"]:
        if issue["severity"] in {"error", "fatal"}:
            assert issue["severity"] == "error", issue
            identifiers = [
                e["valueCode"]
                for e in issue.get("extension", [])
                if e["url"] == "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"
            ]
            assert len(identifiers) == 1, issue
            actual[identifiers[0]] += 1
    assert dict(actual) == expected
    assert data["findings"]["errors"] == sum(expected.values())


def pointer_value(value, pointer):
    """Read an RFC 6901 pointer from fixture JSON."""
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def project_config(root: Path, packages, default=None):
    """Write the common minimal test project configuration."""
    path = root / "specfhir.toml"
    path.write_text(
        f"packages={json.dumps(packages)}\ndefault_package={json.dumps(default or packages[0])}\n"
    )
    return path


def archive(cache, key, resources=(), deps=None, release="4.0.1"):
    cache.mkdir(parents=True, exist_ok=True)
    name, version = key.split("#")
    values = {
        "package/package.json": {
            "name": name,
            "version": version,
            "fhirVersions": [release],
            "dependencies": deps or {},
        }
    }
    values.update({f"package/{r['id']}.json": r for r in resources})
    with tarfile.open(cache / f"{key}.tgz", "w:gz") as output:
        for path, value in values.items():
            content = json.dumps(value).encode()
            member = tarfile.TarInfo(path)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))


def profile(name="PatientProfile", **extra):
    return {
        "resourceType": "StructureDefinition",
        "id": name,
        "name": name,
        "url": f"https://example.org/{name}",
        "version": "1.0.0",
        "type": "Patient",
        "snapshot": {
            "element": [
                {"id": "Patient", "path": "Patient", "min": 0, "max": "*"},
                {"id": "Patient.id", "path": "Patient.id", "min": 0, "max": "1"},
                {"id": "Patient.identifier", "path": "Patient.identifier", "min": 1, "max": "*"},
                {
                    "id": "Patient.identifier:mrn",
                    "path": "Patient.identifier",
                    "sliceName": "mrn",
                    "min": 1,
                    "max": "1",
                },
            ]
        },
        "differential": {"element": [{"id": "Patient", "path": "Patient"}]},
        **extra,
    }
