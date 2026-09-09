import json
import time
from pathlib import Path
from typing import Annotated

import typer

from specfhir import index, packages, search, validator
from specfhir.models import invoke

app = typer.Typer(no_args_is_help=True, help="Local, source-backed FHIR package knowledge.")
package_app = typer.Typer(help="Published releases and installed package coverage.")
app.add_typer(package_app, name="packages")

ConfigOption = Annotated[Path, typer.Option("--config", help="Project configuration file")]


def emit(operation, as_json: bool):
    result = invoke(operation)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
    elif "checks" in result:
        typer.echo(f"{result['status']}: {result['counts']}")
        for check in result["checks"]:
            detail = "; ".join(check["details"])
            typer.echo(f"  {check['status']}: {check['name']}" + (f" — {detail}" if detail else ""))
    elif result.get("inventory"):
        typer.echo(f"{result['status']}: {result['counts']}")
        for item in result["inventory"]:
            detail = item["excluded_reason"] or str(item["artifacts"]) + " artifacts"
            typer.echo(f"  {item['key']}: {detail}")
            if "reference_counts" in item:
                typer.echo(f"    references: {item['reference_counts']}")
        if "reference_checks" in result:
            coverage = result["reference_checks"]
            typer.echo(f"Reference coverage: {coverage.get('counts', coverage)}")
    else:
        typer.echo(json.dumps(result, indent=2), err=result["status"] == "error")
    if result.get("data", {}).get("execution") == "completed":
        if result["data"]["findings"]["errors"]:
            raise typer.Exit(4)
    code = {"error": 1, "not_found": 2, "effective_definition_unavailable": 2, "ambiguous": 3}
    if result["status"] in code:
        raise typer.Exit(code[result["status"]])


@app.command()
def sync(
    config: ConfigOption = Path("specfhir.toml"),
    update_lock: bool = False,
    with_validator: bool = False,
    rebuild: bool = False,
    prune_cache: bool = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Download pinned packages and atomically rebuild the structured index."""

    def operation():
        started = time.monotonic()
        result = index.sync(config, update_lock=update_lock, rebuild=rebuild, prune=prune_cache)
        if with_validator:
            stage = time.monotonic()
            result["validator"] = validator.refresh(config)
            result["timings"]["validator_refresh_seconds"] = round(time.monotonic() - stage, 3)
        result["timings"]["command_total_seconds"] = round(time.monotonic() - started, 3)
        return result

    emit(operation, as_json)


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
    """Serve resolve, inspect, search, and validate over local MCP stdio."""
    from specfhir.mcp import create_server

    create_server(config.resolve()).run(transport="stdio")


@app.command("validator-setup")
def validator_setup(
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Prepare the verified package snapshot for the Compose validator service."""
    emit(lambda: validator.setup(config), as_json)


@app.command("validate")
def validate_command(
    instance: Path,
    package: str | None = None,
    profile: str | None = None,
    terminology_mode: str = "offline",
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Validate a JSON instance with HL7; offline terminology is limited."""

    def operation():
        return validator.validate(
            validator.read_instance(instance),
            package=package,
            profile=profile,
            terminology_mode=terminology_mode,
            config_path=config,
        )

    emit(operation, as_json)


@package_app.command("versions")
def package_versions(name: str, as_json: Annotated[bool, typer.Option("--json")] = False):
    """List registry releases without selecting or installing one."""
    emit(lambda: packages.versions(name), as_json)


@package_app.command("list")
def package_list(
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Show published coverage, lock consistency, and validator readiness."""
    emit(lambda: packages.inventory(config), as_json)


@app.command("validate-cases")
def validate_cases_command(
    manifest: Path,
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Validate a JSON manifest of named instances, exact packages, and profiles."""
    emit(lambda: validator.validate_cases(manifest, config), as_json)


@package_app.command("pages")
def package_pages(
    package: str,
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Preview narrative page selection from the locked ImplementationGuide metadata."""
    emit(lambda: packages.pages(package, config), as_json)


@app.command("check")
def check_command(
    config: ConfigOption = Path("specfhir.toml"),
    package: str | None = None,
    with_validator: bool = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Check installed readiness and publication coverage without rebuilding."""
    from specfhir import checks

    emit(lambda: checks.run(config, package, with_validator), as_json)
