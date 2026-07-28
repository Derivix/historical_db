"""
Shared pytest fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_CSV = FIXTURES_DIR / "sample_options.csv"


@pytest.fixture
def sample_csv_path() -> Path:
    """Return path to the synthetic sample CSV fixture."""
    assert SAMPLE_CSV.exists(), f"Fixture not found: {SAMPLE_CSV}"
    return SAMPLE_CSV


@pytest.fixture
def default_profile():
    """Load the default column mapping profile."""
    from app.config import load_profile
    return load_profile("default")


@pytest.fixture
def gfd_profile():
    """Load the GFD column mapping profile."""
    from app.config import load_profile
    return load_profile("gfd")


@pytest.fixture
def sample_profile():
    """
    A minimal intraday profile that matches the sample_options.csv headers exactly.
    (Same as gfd but named separately for test clarity.)
    """
    from app.config import ColumnMapProfile
    return ColumnMapProfile(
        granularity="intraday",
        timezone="Asia/Kolkata",
        datetime_format="%m/%d/%Y",
        column_map={
            "ticker": ["ticker"],
            "date": ["date"],
            "time": ["time"],
            "open": ["open"],
            "high": ["high"],
            "low": ["low"],
            "close": ["close"],
            "volume": ["volume"],
            "open_interest": ["open interest"],
        }
    )


@pytest.fixture
def db_connection(monkeypatch):
    """
    Attempt to yield a real DB connection; skip the test if unavailable.
    Tests that use this fixture will be skipped in CI without a DB.
    """
    pytest.importorskip("psycopg")
    from app.config import get_settings
    settings = get_settings()

    try:
        import psycopg
        conn = psycopg.connect(settings.database.dsn, autocommit=False)
        yield conn
        conn.rollback()
        conn.close()
    except Exception as exc:
        pytest.skip(f"Database unavailable: {exc}")
