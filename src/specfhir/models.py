"""SpecFHIR boundaries; FHIR resources remain unmodified JSON objects."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Error(ValueError):
    """An actionable configuration, package, or lookup error."""


class PackagePin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: list[str]


class Lock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1] = 1
    roots: list[str]
    packages: list[PackagePin]
    embedding: dict[str, Any] | None = None


class Result(BaseModel):
    status: Literal["ok", "not_found", "ambiguous", "effective_definition_unavailable", "error"]
    context: str | None = None
    data: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None


def invoke(operation) -> dict[str, Any]:
    """One serialization/error boundary for the CLI and MCP adapters."""
    import tarfile

    import httpx
    import psycopg

    try:
        result = operation()
        return result.model_dump(exclude_none=True) if isinstance(result, Result) else result
    except (ValueError, OSError, httpx.HTTPError, psycopg.Error, tarfile.TarError) as exc:
        message = str(exc)
        if isinstance(exc, psycopg.OperationalError):
            message = "PostgreSQL unavailable. Run docker compose up -d --wait; check SPECFHIR_DSN."
        return {"status": "error", "message": message}
