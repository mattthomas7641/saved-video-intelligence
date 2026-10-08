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

import pytest
from sqlmodel import SQLModel

import app.models  # noqa: F401 - registers Video/Action on SQLModel.metadata
from app.db import engine, get_session, init_db


@pytest.fixture(autouse=True)
def clean_db():
    """Every test starts from an empty, freshly-migrated schema."""
    SQLModel.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()
