"""Shared synthetic package builders for isolated tests."""

import io
import json
import tarfile


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
