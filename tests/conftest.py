"""Shared test fixtures.

Points the whole app at an isolated temp directory via SCANNER_DATA_DIR
(the same override app/config.py was already built with for manual testing
throughout development - this file is the first place it's used as an
actual pytest fixture instead of an ad hoc shell export). This must happen
before any `app.*` module is imported anywhere, since app/config.py reads
the env var at import time - hence it runs at conftest module level, not
inside a fixture function.
"""
import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="scanner_test_")
os.environ["SCANNER_DATA_DIR"] = _TEST_DATA_DIR
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
os.environ["SVI_DISABLE_TELEGRAM"] = "1"  # no background Bot API polling during tests

import pytest
from sqlmodel import SQLModel

import app.models  # noqa: F401 - registers Video/Action on SQLModel.metadata
from app.config import SECRETS_PATH
from app.db import engine, get_session, init_db


@pytest.fixture(autouse=True)
def clean_db():
    """Every test starts from an empty, freshly-migrated schema AND a clean
    secrets.json - a test that saves an API key / trusted sender / token
    must not leak into the next test's assertions about unconfigured state.
    (Found by a real failure: a trusted-sender test run earlier in the suite
    made a later "refuses without one configured" test fail only when run
    as part of the full suite, not in isolation - this fixture is that fix.)

    alembic_version is dropped too, not just the app's own tables: it isn't
    part of SQLModel.metadata (Alembic creates it itself via raw SQL), so
    without this, init_db() would see the DB already "at head" from a
    previous test and skip recreating video/action entirely."""
    SQLModel.metadata.drop_all(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        conn.commit()
    init_db()
    SECRETS_PATH.unlink(missing_ok=True)
    yield


@pytest.fixture
def session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()
