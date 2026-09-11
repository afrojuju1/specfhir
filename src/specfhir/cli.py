import json
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from specfhir import comparison, index, packages, search, validator
from specfhir.models import Error, invoke

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
    dataset_id: str | None = None,
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
            dataset_id=dataset_id,
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
    dataset_id: str | None = None,
    pointer: str | None = None,
    offset: int = 0,
    limit: int = 5,
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Inspect metadata, elements, raw JSON, passages, or bounded incoming references."""
    emit(
        lambda: search.inspect(
            selector,
            package=package,
            artifact_version=artifact_version,
            element=element,
            view=view,
            dataset_id=dataset_id,
            pointer=pointer,
            offset=offset,
            limit=limit,
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
    dataset_id: str | None = None,
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
            dataset_id=dataset_id,
            config_path=config,
        ),
        as_json,
    )


@app.command()
def mcp(config: ConfigOption = Path("specfhir.toml")):
    """Serve knowledge discovery, comparison, retrieval and validation over MCP stdio."""
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
    package: Annotated[
        list[str] | None, typer.Option(help="Repeat for multiple exact packages")
    ] = None,
    profile: str | None = None,
    contexts: Annotated[
        str | None, typer.Option(help="JSON array of package/profile selections")
    ] = None,
    dataset_id: str | None = None,
    terminology_mode: str = "offline",
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Validate once per selected context; compare findings across any selected releases."""

    def operation():
        selection: dict[str, Any]
        if contexts is not None:
            if package or profile is not None:
                raise Error("Use --contexts or --package/--profile, not both")
            selections = json.loads(contexts)
            if not isinstance(selections, list):
                raise Error("--contexts must be a JSON array")
            selection = {"contexts": selections}
        elif package and len(package) > 1:
            selection = {"contexts": [{"package": p, "profile": profile} for p in package]}
        else:
            selection = {"package": package[0] if package else None, "profile": profile}
        return validator.validate(
            validator.read_instance(instance),
            **selection,
            terminology_mode=terminology_mode,
            dataset_id=dataset_id,
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


@app.command("contexts")
def contexts_command(
    package: str | None = None,
    offset: int = 0,
    limit: int = 50,
    dataset_id: str | None = None,
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Discover installed package contexts and actual published dataset identity."""
    emit(
        lambda: packages.contexts(
            config, package=package, offset=offset, limit=limit, dataset_id=dataset_id
        ),
        as_json,
    )


@app.command("compare")
def compare_command(
    left_package: Annotated[str, typer.Option()],
    right_package: Annotated[str, typer.Option()],
    selector: Annotated[str | None, typer.Argument()] = None,
    mode: str = "profile",
    right_selector: str | None = None,
    left_artifact_version: str | None = None,
    right_artifact_version: str | None = None,
    view: str = "snapshot",
    element: str | None = None,
    offset: int = 0,
    limit: int = 50,
    dataset_id: str | None = None,
    config: ConfigOption = Path("specfhir.toml"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Compare profiles, package inventories or direct reference targets in exact contexts."""
    emit(
        lambda: comparison.compare(
            selector,
            left_package=left_package,
            right_package=right_package,
            mode=mode,
            right_selector=right_selector,
            left_artifact_version=left_artifact_version,
            right_artifact_version=right_artifact_version,
            view=view,
            element=element,
            offset=offset,
            limit=limit,
            dataset_id=dataset_id,
            config_path=config,
        ),
        as_json,
    )
