import json
from pathlib import Path
from typing import Annotated

import typer

from specfhir import index, search
from specfhir.models import invoke

app = typer.Typer(no_args_is_help=True, help="Local, source-backed FHIR package knowledge.")
ConfigOption = Annotated[Path, typer.Option("--config", help="Project configuration file")]


def emit(operation, as_json: bool):
    result = invoke(operation)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
    elif result.get("inventory"):
        typer.echo(f"{result['status']}: {result['counts']}")
        for item in result["inventory"]:
            detail = item["excluded_reason"] or str(item["artifacts"]) + " artifacts"
            typer.echo(f"  {item['key']}: {detail}")
    else:
        typer.echo(json.dumps(result, indent=2), err=result["status"] == "error")
    code = {"error": 1, "not_found": 2, "effective_definition_unavailable": 2, "ambiguous": 3}
    if result["status"] in code:
        raise typer.Exit(code[result["status"]])


@app.command()
def sync(
    config: ConfigOption = Path("specfhir.toml"),
    update_lock: bool = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Download pinned packages and atomically rebuild the structured index."""
    emit(lambda: index.sync(config, update_lock=update_lock), as_json)


@app.command()
def resolve(
    selector: str,
    package: str | None = None,
    artifact_version: str | None = None,
    element: str | None = None,
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Resolve an artifact or effective snapshot element exactly."""
    emit(
        lambda: search.resolve(
            selector,
            package=package,
            artifact_version=artifact_version,
            element=element,
            config_path=config,
        ),
        as_json,
    )


@app.command()
def inspect(
    selector: str,
    package: str | None = None,
    artifact_version: str | None = None,
    element: str | None = None,
    view: str = "snapshot",
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Inspect compact metadata, supplied snapshot/differential elements, or raw JSON."""
    emit(
        lambda: search.inspect(
            selector,
            package=package,
            artifact_version=artifact_version,
            element=element,
            view=view,
            config_path=config,
        ),
        as_json,
    )


@app.command("search")
def search_command(
    query: str,
    package: str | None = None,
    resource_type: str | None = None,
    limit: int = 5,
    mode: str = "auto",
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Search package evidence using lexical, semantic, hybrid, or automatic mode."""
    emit(
        lambda: search.search(
            query,
            package=package,
            resource_type=resource_type,
            limit=limit,
            mode=mode,
            config_path=config,
        ),
        as_json,
    )


@app.command()
def mcp(config: ConfigOption = Path("specfhir.toml")):
    """Serve resolve, inspect, and search over local MCP stdio."""
    from specfhir.mcp import create_server

    create_server(config.resolve()).run(transport="stdio")
