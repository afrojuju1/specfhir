"""Explicit SQL and transaction boundaries for the disposable local index."""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from specfhir.config import dsn

SCHEMA_VERSION = 5
SYNC_LOCK = 1936746086

# Shared source-package closure; callers retain their query and transaction boundaries.
SCOPE = """WITH RECURSIVE scope(key) AS (
    SELECT key FROM packages WHERE key=%(package)s
    UNION
    SELECT dependency_key FROM package_dependencies d JOIN scope s ON d.package_key=s.key
)"""

DDL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
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
CREATE TABLE IF NOT EXISTS excluded_artifacts (
    package_key text NOT NULL REFERENCES packages(key),
    file_path text NOT NULL,
    canonical text NOT NULL,
    version text,
    resource_type text NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY (package_key, file_path)
);
CREATE INDEX IF NOT EXISTS excluded_canonical ON excluded_artifacts(canonical);
CREATE TABLE IF NOT EXISTS artifact_references (
    artifact_id bigint NOT NULL REFERENCES artifacts(id),
    pointer text NOT NULL,
    relationship text NOT NULL,
    target text NOT NULL,
    status text NOT NULL,
    detail jsonb NOT NULL,
    PRIMARY KEY (artifact_id, pointer)
);
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
CREATE TABLE IF NOT EXISTS documents (
    artifact_id bigint NOT NULL REFERENCES artifacts(id),
    kind text NOT NULL,
    pointer text NOT NULL,
    element_id text,
    representation text,
    chunk integer NOT NULL,
    heading text NOT NULL,
    text text NOT NULL,
    text_hash text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', heading), 'A') ||
        setweight(to_tsvector('english', text), 'B')
    ) STORED,
    PRIMARY KEY (artifact_id, pointer, chunk)
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding public.vector(384);
CREATE INDEX IF NOT EXISTS documents_fts ON documents USING gin(search_vector);
CREATE INDEX IF NOT EXISTS artifacts_fuzzy ON artifacts USING gin
    ((coalesce(name, '') || ' ' || coalesce(title, '')) public.gin_trgm_ops);

"""


def connect():
    return psycopg.Connection[dict[str, Any]].connect(
        dsn(), autocommit=True, row_factory=dict_row, connect_timeout=5
    )
