import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("SPECFHIR_TEST_DSN")
    if not dsn:
        pytest.skip("Set SPECFHIR_TEST_DSN to run PostgreSQL acceptance tests")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        monkeypatch.setenv("SPECFHIR_DSN", make_conninfo(dsn, options=f"-c search_path={schema}"))
        try:
            yield
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
