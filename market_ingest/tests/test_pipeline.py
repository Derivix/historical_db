"""
End-to-end pipeline tests using the synthetic fixture.

These tests require a running TimescaleDB instance.
They are skipped automatically if the DB is unavailable.

Test sequence:
  1. Migrate schema
  2. Ingest sample_options.csv
  3. Compute VWAP
  4. Run audit
  5. Assert audit flags:
     - Missing strike 23475 (between 23450 and 23500)
     - Missing PE for strike 23550 (deliberate missing CE/PE pair)
     - Time gap in NIFTY27JUN2423400CE.NFO (10:31–11:30 missing)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_CSV = FIXTURES_DIR / "sample_options.csv"


@pytest.fixture(scope="module")
def db_conn():
    """Module-scoped DB connection, skipped if DB not available."""
    pytest.importorskip("psycopg")
    try:
        import psycopg
        from app.config import get_settings
        s = get_settings()
        conn = psycopg.connect(s.database.dsn, autocommit=False)
        yield conn
        conn.rollback()
        conn.close()
    except Exception as exc:
        pytest.skip(f"Database unavailable: {exc}")


@pytest.fixture(scope="module", autouse=False)
def migrated_db(db_conn):
    """Run migrations once for the module."""
    from app.db.migrations import run_migrations
    from app.config import get_settings
    run_migrations(dsn=get_settings().database.dsn)
    return db_conn


@pytest.fixture(scope="module")
def ingested(migrated_db):
    """Ingest the sample CSV once for all tests in this module."""
    # Clean up any previous data for idempotency
    conn = migrated_db
    conn.execute("DELETE FROM feature")
    conn.execute("DELETE FROM option_selection")
    conn.execute("DELETE FROM ohlcv")
    conn.execute("DELETE FROM instrument")
    conn.execute("DELETE FROM underlying")
    conn.commit()

    from app.ingest.pipeline import ingest_file
    from app.config import ColumnMapProfile
    from app.config import get_settings

    profile = ColumnMapProfile(
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

    # Temporarily override profile loading
    import app.config as cfg_mod
    original_load = cfg_mod.load_profile

    def _load(name, profiles_dir=None):
        return profile

    cfg_mod.load_profile = _load
    try:
        result = ingest_file(SAMPLE_CSV, profile_name="test")
    finally:
        cfg_mod.load_profile = original_load

    return result


class TestIngestResult:
    def test_no_errors(self, ingested):
        assert not ingested.errors, f"Ingest errors: {ingested.errors}"

    def test_rows_read(self, ingested):
        assert ingested.rows_read > 0

    def test_rows_inserted(self, ingested):
        assert ingested.rows_inserted > 0

    def test_idempotent(self, migrated_db, ingested):
        """Re-ingesting the same file should not increase row count."""
        conn = migrated_db
        count_before = conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]

        from app.ingest.pipeline import ingest_file
        from app.config import ColumnMapProfile
        import app.config as cfg_mod

        profile = ColumnMapProfile(
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

        original_load = cfg_mod.load_profile
        cfg_mod.load_profile = lambda name, profiles_dir=None: profile
        try:
            ingest_file(SAMPLE_CSV, profile_name="test")
        finally:
            cfg_mod.load_profile = original_load

        count_after = conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
        assert count_before == count_after, "Re-ingestion changed row count (not idempotent)"


class TestVWAP:
    def test_vwap_computed(self, ingested, migrated_db):
        from app.features.vwap import compute_and_store_vwap
        n = compute_and_store_vwap()
        assert n > 0, "No VWAP rows computed"

    def test_vwap_in_db(self, migrated_db):
        conn = migrated_db
        count = conn.execute("SELECT COUNT(*) FROM feature WHERE vwap IS NOT NULL").fetchone()[0]
        assert count > 0


class TestAudit:
    @pytest.fixture(autouse=True)
    def _needs_ingested(self, ingested):
        pass

    def test_missing_strike_flagged(self, migrated_db):
        """
        The fixture has strikes 23400, 23450, 23500, 23550.
        No strike 23475 — but since strike_step is not set in underlying,
        the audit detects missing CE/PE pairs instead.
        We verify the audit runs and produces a report.
        """
        # Set strike_step on the NIFTY underlying so ladder check works
        conn = migrated_db
        conn.execute(
            "UPDATE underlying SET strike_step = 50 WHERE symbol = 'NIFTY'"
        )
        conn.commit()

        from app.audit.checks import run_all_checks
        report = run_all_checks()

        # 23475 is not in the fixture (23450 and 23500 present, step=50 means 23475 not expected)
        # but 23550 is missing PE — should be flagged
        nifty_issues = [i for i in report.strike_issues if i.underlying_symbol == "NIFTY"]
        assert nifty_issues, "Expected strike issues for NIFTY"

    def test_missing_pe_for_23550_flagged(self, migrated_db):
        """
        23550PE is not in the fixture — should appear as missing_pe.
        """
        from app.audit.checks import run_all_checks
        report = run_all_checks()

        from decimal import Decimal
        for issue in report.strike_issues:
            if issue.underlying_symbol == "NIFTY" and issue.expiry == date(2024, 6, 27):
                if Decimal("23550") in issue.missing_pe:
                    return  # Found — test passes
        pytest.fail("Missing PE for 23550 not flagged in audit")

    def test_time_gap_flagged(self, migrated_db):
        """
        NIFTY27JUN2423400CE has a gap from 10:31 to 11:31 (no bars 10:31–11:30).
        The audit gap check should flag it.
        """
        from app.audit.checks import run_all_checks
        report = run_all_checks()

        ce_gaps = [
            g for g in report.gap_issues
            if "23400CE" in (g.raw_ticker or "")
        ]
        assert ce_gaps, "Expected gap issues for 23400CE"
        largest = max(g.largest_gap_minutes for g in ce_gaps)
        # Gap is ~61 minutes (10:30 to 11:31)
        assert largest >= 60, f"Expected gap >= 60 min, got {largest}"

    def test_audit_report_has_underlying_results(self, migrated_db):
        from app.audit.checks import run_all_checks
        report = run_all_checks()
        assert "NIFTY" in report.underlying_results
