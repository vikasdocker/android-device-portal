"""Shared pytest fixtures for the portal test suite."""
from __future__ import annotations

import pytest

from portal.app import config, store


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Point the SQLite store at a temporary directory and reset its connection."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "portal.db")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")

    store._conn = None
    store.init()
    try:
        yield store
    finally:
        if store._conn is not None:
            store._conn.close()
        store._conn = None
