import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from helpers import archive, profile
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from typer.testing import CliRunner

from specfhir import db, documents, index, search
from specfhir.cli import app
from specfhir.models import Error


def test_source_text_and_chunks():
    r = profile(
        description="Patient guidance",
        text={
            "div": (
                "<div><p>First &amp; second</p><script>untrusted()</script>"
                "<style>hidden</style><p>Third</p></div>"
            )
        },
    )
    r["snapshot"]["element"][2]["requirements"] = "Patient identifiers are required."
    docs = list(documents.extract(r, {}))
    narrative = next(d for d in docs if d[0] == "narrative")
    assert narrative[1] == "/text/div"
    assert narrative[6] == "First & second\n\nThird"
    element = next(d for d in docs if d[0] == "element")
    assert element[1:4] == ["/snapshot/element/2", "Patient.identifier", "snapshot"]
    assert not any(
        d[0] == "element" for d in documents.extract(r, {"snapshot": "bad", "differential": "bad"})
    )
    original = "x" * 10001
    parts = list(documents.chunks(original))
    assert "".join(parts) == original and max(map(len, parts)) <= documents.MAX_CHARS


def test_search_and_stdio_parity(tmp_path, database, monkeypatch):
    cache = tmp_path / ".specfhir/packages"
    patient = profile(description="Patient identifier requirements.", title="Patient identity")
    patient["snapshot"]["element"][2]["requirements"] = "Patients need identifiers for matching."
    archive(cache, "example.patient#1.0.0", [patient])
    archive(
        cache, "example.other#1.0.0", [profile("Other", description="Secret unrelated evidence")]
    )
    config = tmp_path / "specfhir.toml"
    config.write_text(
        'packages=["example.patient#1.0.0","example.other#1.0.0"]\ndefault_package="example.patient#1.0.0"\n'
    )
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    index.sync(config)
    result = search.search("patient identifier requirements", config_path=config)
    assert result.data["results"][0]["source"]["package"] == "example.patient#1.0.0"
    assert search.search("secret unrelated", config_path=config).data["results"] == []
    assert (
        search.search("patient", resource_type="ValueSet", config_path=config).data["results"] == []
    )
    fuzzy = search.search("Patint identty", config_path=config)
    assert fuzzy.data["results"][0]["method"] == "fuzzy"
    assert search.search("the and", config_path=config).data["results"] == []
    assert search.search("patient", limit=1, config_path=config).data["results"]
    with pytest.raises(Error, match="limit"):
        search.search("patient", limit=0, config_path=config)
    assert index.sync(config)["status"] == "unchanged"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] > 0

    async def check_stdio():
        params = StdioServerParameters(
            command=shutil.which("uv"),
            args=["run", "--no-sync", "specfhir", "mcp", "--config", str(config)],
            cwd=Path(__file__).resolve().parents[1],
            env={"SPECFHIR_DSN": os.environ["SPECFHIR_DSN"]},
        )
        async with Client(params, read_timeout_seconds=15) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {
                "resolve",
                "inspect",
                "search",
                "validate",
                "contexts",
                "compare",
            }
            cases = [
                ("contexts", {}, []),
                (
                    "compare",
                    {
                        "selector": "PatientProfile",
                        "left_package": "example.patient#1.0.0",
                        "right_package": "example.patient#1.0.0",
                    },
                    [
                        "PatientProfile",
                        "--left-package",
                        "example.patient#1.0.0",
                        "--right-package",
                        "example.patient#1.0.0",
                    ],
                ),
                (
                    "compare",
                    {
                        "selector": "missing",
                        "left_package": "example.patient#1.0.0",
                        "right_package": "example.patient#1.0.0",
                    },
                    [
                        "missing",
                        "--left-package",
                        "example.patient#1.0.0",
                        "--right-package",
                        "example.patient#1.0.0",
                    ],
                ),
                ("resolve", {"selector": "Patient.id"}, ["Patient.id"]),
                ("inspect", {"selector": "Patient"}, ["Patient"]),
                (
                    "search",
                    {"query": "patient identifier requirements"},
                    ["patient identifier requirements"],
                ),
                ("resolve", {"selector": "missing"}, ["missing"]),
                ("search", {"query": "patient", "limit": 0}, ["patient", "--limit", "0"]),
            ]
            for name, args, cli_args in cases:
                actual = await client.call_tool(name, args)
                expected = CliRunner().invoke(
                    app, [name, *cli_args, "--config", str(config), "--json"]
                )
                assert actual.structured_content == json.loads(expected.stdout)

    asyncio.run(check_stdio())
