"""Live acceptance reuses normal pytest assertions and the public API."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from specfhir import checks, packages, search, validator
from specfhir.config import dsn
from specfhir.files import checksum
from specfhir.models import Lock, invoke

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "specfhir.toml"


@pytest.fixture(scope="module", autouse=True)
def installed():
    report = checks.run(CONFIG, with_validator=True)
    assert report["status"] == "ok", report


@pytest.fixture
def fhir():
    def read(name):
        return validator.read_instance(ROOT / "tests/fixtures/fhir" / f"{name}.json")

    return read


@pytest.fixture(scope="module")
def published():
    lock = Lock.model_validate_json((ROOT / "specfhir.lock").read_bytes())
    cache = {}

    def read(package, member):
        if package not in cache:
            pin = next(p for p in lock.packages if p.key == package)
            archive = ROOT / ".specfhir/packages" / f"{package}.tgz"
            assert checksum(archive) == pin.sha256
            cache[package] = {
                name: raw
                for name, raw in packages.archive_files(archive)
                if name.startswith("package/example/") and name.endswith(".json")
            }
        return json.loads(cache[package][member])

    return read


@pytest.fixture(scope="module")
def call(tmp_path_factory):
    """Collect API outcomes; compare real CLI and one MCP session after each IG module."""
    responses = []

    def execute(tool, **args):
        operation = validator.validate if tool == "validate" else getattr(search, tool)
        result = invoke(lambda: operation(**args, config_path=CONFIG))
        assert result["status"] != "error", result
        responses.append((tool, args, result))
        return result

    yield execute
    instance_path = tmp_path_factory.mktemp("acceptance") / "instance.json"
    for tool, args, expected in responses:
        options = dict(args)
        if tool == "validate":
            instance_path.write_text(json.dumps(options.pop("instance")))
            positional = str(instance_path)
        else:
            positional = options.pop("query" if tool == "search" else "selector")
        command = [
            sys.executable,
            "-m",
            "specfhir",
            tool,
            positional,
            "--config",
            str(CONFIG),
            "--json",
        ]
        for name, value in options.items():
            if value is not None:
                command.extend(["--" + name.replace("_", "-"), str(value)])
        output = subprocess.run(command, capture_output=True, text=True, timeout=240)
        expected_code = (
            2
            if expected["status"] == "not_found"
            else (4 if expected.get("data", {}).get("findings", {}).get("errors") else 0)
        )
        assert output.returncode == expected_code, output.stderr
        assert json.loads(output.stdout) == expected

    async def parity():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "specfhir", "mcp", "--config", str(CONFIG)],
            env={"SPECFHIR_DSN": dsn()},
        )
        async with Client(params, read_timeout_seconds=240) as client:
            for tool, args, expected in responses:
                result = await client.call_tool(tool, args)
                assert result.structured_content == expected

    asyncio.run(parity())
