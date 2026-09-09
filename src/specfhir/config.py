import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from specfhir.models import Error

PACKAGE = re.compile(r"[a-z0-9][a-z0-9.-]*#[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?")
DEFAULT_DSN = "postgresql://specfhir@localhost:55432/specfhir"


def split_key(key: str) -> tuple[str, str]:
    if not PACKAGE.fullmatch(key):
        raise Error(f"Expected name#exact-version, got {key!r}; version ranges are unsupported")
    name, version = key.split("#")
    return name, version


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    model: Literal["BAAI/bge-small-en-v1.5"] = "BAAI/bge-small-en-v1.5"
    revision: str = Field(
        default="52398278842ec682c6f32300af41344b1c0b0bb2", pattern=r"^[0-9a-f]{40}$"
    )
    max_tokens: int = Field(default=256, ge=128, le=512)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    packages: list[str] = Field(min_length=1)
    default_package: str
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    @model_validator(mode="after")
    def check_packages(self):
        for key in self.packages:
            split_key(key)
        if len(set(self.packages)) != len(self.packages):
            raise Error("Duplicate configured packages")
        if self.default_package not in self.packages:
            raise Error("default_package must be a configured root")
        return self


def load(path: Path) -> Config:
    with path.open("rb") as stream:
        return Config.model_validate(tomllib.load(stream))


def dsn() -> str:
    return os.environ.get("SPECFHIR_DSN", DEFAULT_DSN)
