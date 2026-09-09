import json
import tarfile
from pathlib import Path
from typing import Annotated

import httpx
import psycopg
import typer

from specfhir import index, search
from specfhir.models import Result

app = typer.Typer(no_args_is_help=True, help="Local, source-backed FHIR package knowledge.")
ConfigOption = Annotated[Path, typer.Option("--config", help="Project configuration file")]


def emit(operation, as_json: bool):
    try:
        result = operation()
        if isinstance(result, Result):
            result = result.model_dump(exclude_none=True)
        if as_json:
            typer.echo(json.dumps(result, indent=2))
        elif result.get("inventory"):
            typer.echo(f"{result['status']}: {result['counts']}")
            for item in result["inventory"]:
                detail = item["excluded_reason"] or str(item["artifacts"]) + " artifacts"
                typer.echo(f"  {item['key']}: {detail}")
        else:
            typer.echo(json.dumps(result, indent=2))
        status = result["status"]
        if status in {"not_found", "effective_definition_unavailable"}:
            raise typer.Exit(2)
        if status == "ambiguous":
            raise typer.Exit(3)
    except (ValueError, OSError, httpx.HTTPError, psycopg.Error, tarfile.TarError) as exc:
        message = str(exc)
        if isinstance(exc, psycopg.OperationalError):
            message = "PostgreSQL unavailable. Run docker compose up -d --wait; check SPECFHIR_DSN."
        if as_json:
            typer.echo(json.dumps({"status": "error", "message": message}))
        else:
            typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(1) from exc


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
