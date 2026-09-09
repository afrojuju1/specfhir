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


class Result(BaseModel):
    status: Literal["ok", "not_found", "ambiguous", "effective_definition_unavailable", "error"]
    context: str | None = None
    data: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None
