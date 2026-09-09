"""Explicit SQL and transaction boundaries for the disposable local index."""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from specfhir.config import dsn

SCHEMA_VERSION = 1
SYNC_LOCK = 1936746086

DDL = """
CREATE TABLE IF NOT EXISTS index_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    identity text NOT NULL,
    metadata jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    key text PRIMARY KEY,
    manifest jsonb NOT NULL,
    sha256 text NOT NULL,
    excluded_reason text
);
CREATE TABLE IF NOT EXISTS package_dependencies (
    package_key text REFERENCES packages(key),
    dependency_key text REFERENCES packages(key),
    PRIMARY KEY (package_key, dependency_key)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id bigint PRIMARY KEY,
    package_key text NOT NULL REFERENCES packages(key),
    file_path text NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    canonical text,
    version text,
    name text,
    title text,
    resource jsonb NOT NULL,
    projection_issues jsonb NOT NULL,
    UNIQUE (package_key, file_path)
);
CREATE INDEX IF NOT EXISTS artifacts_package ON artifacts(package_key);
CREATE INDEX IF NOT EXISTS artifacts_canonical ON artifacts(canonical);
CREATE INDEX IF NOT EXISTS artifacts_name ON artifacts(name);
CREATE INDEX IF NOT EXISTS artifacts_resource_id ON artifacts(resource_id);
CREATE TABLE IF NOT EXISTS elements (
    artifact_id bigint NOT NULL REFERENCES artifacts(id),
    representation text NOT NULL CHECK (representation IN ('snapshot', 'differential')),
    element_id text NOT NULL,
    path text NOT NULL,
    slice_name text,
    ordinal integer NOT NULL,
    element jsonb NOT NULL,
    PRIMARY KEY (artifact_id, representation, element_id),
    UNIQUE (artifact_id, representation, ordinal)
);
CREATE INDEX IF NOT EXISTS elements_path ON elements(artifact_id, representation, path);
"""


def connect():
    return psycopg.Connection[dict[str, Any]].connect(
        dsn(), autocommit=True, row_factory=dict_row, connect_timeout=5
    )
