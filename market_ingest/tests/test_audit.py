"""
Unit tests for individual audit check functions.

These tests use mock DB connections (simple objects with an execute method)
so they can run without a real database.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.audit.checks import (
    AuditReport,
    GapIssue,
    OIIssue,
    OrphanIssue,
    StrikeCoverageIssue,
    FeatureCoverageIssue,
    _build_ladder,
    _find_gaps,
    _expected_bars_per_day,
)


# ---------------------------------------------------------------------------
# _build_ladder
# ---------------------------------------------------------------------------

class TestBuildLadder:
    def test_simple_ladder(self):
        result = _build_ladder(Decimal("100"), Decimal("200"), Decimal("50"))
        assert result == {Decimal("100"), Decimal("150"), Decimal("200")}

    def test_single_step(self):
        result = _build_ladder(Decimal("23500"), Decimal("23500"), Decimal("50"))
        assert result == {Decimal("23500")}

    def test_nifty_like_ladder(self):
        result = _build_ladder(Decimal("23400"), Decimal("23550"), Decimal("50"))
        assert Decimal("23400") in result
        assert Decimal("23450") in result
        assert Decimal("23500") in result
        assert Decimal("23550") in result
        assert len(result) == 4

    def test_fine_grained(self):
        result = _build_ladder(Decimal("100"), Decimal("110"), Decimal("5"))
        assert len(result) == 3  # 100, 105, 110


# ---------------------------------------------------------------------------
# _expected_bars_per_day
# ---------------------------------------------------------------------------

class TestExpectedBars:
    def test_standard_session(self):
        # 09:15 to 15:30 = 375 minutes → 376 bars (inclusive both ends)
        n = _expected_bars_per_day(9, 15, 15, 30, 1)
        assert n == 376

    def test_single_bar(self):
        n = _expected_bars_per_day(9, 15, 9, 15, 1)
        assert n >= 1


# ---------------------------------------------------------------------------
# _find_gaps
# ---------------------------------------------------------------------------

class TestFindGaps:
    def _ts(self, h: int, m: int) -> datetime:
        """Create a UTC timestamp at the given IST hour:minute on 2024-05-02."""
        # IST = UTC+5:30, so IST 09:15 = UTC 03:45
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        dt = datetime(2024, 5, 2, h, m, 0, tzinfo=IST)
        return dt.astimezone(timezone.utc)

    def test_no_gap(self):
        timestamps = [self._ts(9, i) for i in range(15, 20)]
        count, largest = _find_gaps(timestamps, gran=1, start_h=9, start_m=15, end_h=15, end_m=30)
        assert count == 0
        assert largest == 0.0

    def test_one_gap(self):
        timestamps = [
            self._ts(9, 15),
            self._ts(9, 16),
            self._ts(9, 17),
            # gap: 9:17 → 10:30 (73 minutes)
            self._ts(10, 30),
            self._ts(10, 31),
        ]
        count, largest = _find_gaps(timestamps, gran=1, start_h=9, start_m=15, end_h=15, end_m=30)
        assert count == 1
        assert largest == pytest.approx(73.0, abs=1.0)

    def test_cross_day_not_counted(self):
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        # Two different dates
        ts1 = datetime(2024, 5, 2, 15, 30, 0, tzinfo=IST).astimezone(timezone.utc)
        ts2 = datetime(2024, 5, 3, 9, 15, 0, tzinfo=IST).astimezone(timezone.utc)
        count, largest = _find_gaps([ts1, ts2], gran=1, start_h=9, start_m=15, end_h=15, end_m=30)
        assert count == 0  # cross-day not counted


# ---------------------------------------------------------------------------
# AuditReport serialisation
# ---------------------------------------------------------------------------

class TestAuditReportSerialization:
    def test_to_dict_empty(self):
        report = AuditReport()
        d = report.to_dict()
        assert "strike_issues" in d
        assert "gap_issues" in d
        assert d["critical_failures"] == []

    def test_to_dict_with_data(self):
        report = AuditReport()
        report.strike_issues.append(StrikeCoverageIssue(
            underlying_symbol="NIFTY",
            expiry=date(2024, 6, 27),
            missing_strikes=[Decimal("23475")],
            missing_ce=[],
            missing_pe=[Decimal("23550")],
        ))
        report.gap_issues.append(GapIssue(
            instrument_id=1,
            raw_ticker="NIFTY27JUN2423400CE.NFO",
            gap_count=1,
            largest_gap_minutes=61.0,
            gap_pct=3.5,
        ))
        report.critical_failures.append("1 underlying(s) have missing strikes")

        d = report.to_dict()
        assert len(d["strike_issues"]) == 1
        assert d["strike_issues"][0]["underlying"] == "NIFTY"
        assert 23475.0 in d["strike_issues"][0]["missing_strikes"]
        assert 23550.0 in d["strike_issues"][0]["missing_pe_strikes"]
        assert len(d["gap_issues"]) == 1
        assert d["gap_issues"][0]["largest_gap_minutes"] == 61.0
        assert d["critical_failures"]

    def test_has_critical_failures(self):
        report = AuditReport()
        assert not report.has_critical_failures()
        report.critical_failures.append("something bad")
        assert report.has_critical_failures()


# ---------------------------------------------------------------------------
# StrikeCoverageIssue properties
# ---------------------------------------------------------------------------

class TestStrikeCoverageIssue:
    def test_missing_pe_detected(self):
        issue = StrikeCoverageIssue(
            underlying_symbol="NIFTY",
            expiry=date(2024, 6, 27),
            missing_strikes=[],
            missing_ce=[],
            missing_pe=[Decimal("23550")],
        )
        assert Decimal("23550") in issue.missing_pe
        assert issue.missing_ce == []


# ---------------------------------------------------------------------------
# GapIssue properties
# ---------------------------------------------------------------------------

class TestGapIssue:
    def test_gap_pct_calculation(self):
        # Just verify the dataclass stores correctly
        issue = GapIssue(
            instrument_id=42,
            raw_ticker="NIFTY.NSE",
            gap_count=10,
            largest_gap_minutes=61.0,
            gap_pct=2.66,
        )
        assert issue.gap_pct == pytest.approx(2.66)
        assert issue.largest_gap_minutes == 61.0
