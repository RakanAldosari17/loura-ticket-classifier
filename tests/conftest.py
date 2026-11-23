"""Shared test fixtures: a temporary database, a scripted fake model, a client.

Fixtures live here rather than being copied into each test file, so every test
gets the same clean, isolated starting point.
"""

import pytest

from app import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch) -> str:
    """Point the whole storage layer at an empty database file for one test.

    `tmp_path` is a fresh directory per test, so tests cannot see each other's
    tickets and can run in any order. `monkeypatch` sets the environment
    variable that `config.load_settings()` reads, and undoes it afterwards; this
    works because `db._connect()` asks for the path on every call rather than
    remembering one from import time.
    """
    database_path = tmp_path / "test_tickets.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    db.init_db()
    return str(database_path)
