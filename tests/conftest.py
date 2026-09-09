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


def pytest_addoption(parser):
    parser.addoption(
        "--live-acceptance",
        action="store_true",
        help="Run read-only acceptance against installed packages and validator",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live-acceptance"):
        skip = pytest.mark.skip(reason="Pass --live-acceptance for installed-system acceptance")
        for item in items:
            if "acceptance" in item.path.parts:
                item.add_marker(skip)
