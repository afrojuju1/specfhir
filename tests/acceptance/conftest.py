"""Live acceptance reuses normal pytest assertions and the public API."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import assert_published_errors
from mcp import Client, StdioServerParameters

from specfhir import checks, comparison, packages, search, validator
from specfhir.config import dsn
from specfhir.files import checksum
from specfhir.models import Lock, invoke

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "specfhir.toml"
PUBLISHED_ERRORS = json.loads((ROOT / "tests/fixtures/published_errors.json").read_bytes())


@pytest.fixture(scope="session", autouse=True)
def installed():
    report = checks.run(CONFIG, with_validator=True)
    assert report["status"] == "ok", report


@pytest.fixture
def fhir():
    def read(name):
        return validator.read_instance(ROOT / "tests/fixtures/fhir" / f"{name}.json")

    return read


@pytest.fixture
def validate_published(call, published, record_property):
    def validate(package, member, profile=None):
        result = call(
            "validate", instance=published(package, member), package=package, profile=profile
        )
        record_property("published_outcome:" + member, json.dumps(result))
        data = result["data"]
        assert data["validator_version"] == PUBLISHED_ERRORS["validator_version"]
        assert_published_errors(data, PUBLISHED_ERRORS["packages"][package][member])
        return result

    return validate


@pytest.fixture(scope="session")
def locked():
    return Lock.model_validate_json((ROOT / "specfhir.lock").read_bytes())


@pytest.fixture(scope="module")
def published(locked):
    lock = locked
    cache = {}

    def read(package, member):
        if package not in cache:
            pin = next(p for p in lock.packages if p.key == package)
            archive = ROOT / ".specfhir/packages" / f"{package}.tgz"
            assert checksum(archive) == pin.sha256
            cache[package] = {
                name: raw
                for name, raw in packages.archive_files(archive)
                if name.startswith("package/") and name.endswith(".json")
            }
        return json.loads(cache[package][member])

    return read


@pytest.fixture(scope="session")
def call(tmp_path_factory):
    """Run every API case; replay one contract per worker-session result class."""
    responses = {}

    def exit_code(result):
        if result["status"] == "error":
            return 1
        if result["status"] in {"not_found", "effective_definition_unavailable"}:
            return 2
        return 4 if result.get("data", {}).get("findings", {}).get("errors") else 0

    def execute(tool, *, expect_error=False, **args):
        operation = {
            "validate": validator.validate,
            "contexts": packages.contexts,
            "compare": comparison.compare,
        }.get(tool) or getattr(search, tool)
        result = invoke(lambda: operation(**args, config_path=CONFIG))
        assert (result["status"] == "error") == expect_error, result
        responses.setdefault((tool, result["status"], exit_code(result)), (tool, args, result))
        return result

    yield execute
    instance_path = tmp_path_factory.mktemp("acceptance") / "instance.json"

    async def parity():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "specfhir", "mcp", "--config", str(CONFIG)],
            env={"SPECFHIR_DSN": dsn()},
        )
        async with Client(params, read_timeout_seconds=240) as client:
            for tool, args, expected in responses.values():
                options = dict(args)
                if tool == "validate":
                    instance_path.write_text(json.dumps(options.pop("instance")))
                    positional = str(instance_path)
                elif tool == "contexts":
                    positional = None
                else:
                    positional = options.pop("query" if tool == "search" else "selector", None)
                command = [
                    sys.executable,
                    "-m",
                    "specfhir",
                    tool,
                    *([positional] if positional is not None else []),
                    "--config",
                    str(CONFIG),
                    "--json",
                ]
                for name, value in options.items():
                    if value is not None:
                        command.extend(
                            [
                                "--" + name.replace("_", "-"),
                                json.dumps(value)
                                if isinstance(value, (list, dict))
                                else str(value),
                            ]
                        )
                output = await asyncio.to_thread(
                    subprocess.run, command, capture_output=True, text=True, timeout=240
                )
                assert output.returncode == exit_code(expected), output.stderr
                assert json.loads(output.stdout) == expected
                result = await client.call_tool(tool, args)
                assert result.structured_content == expected

    asyncio.run(parity())
