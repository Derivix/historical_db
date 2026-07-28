"""
Tests for app.ingest.validator
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ingest.validator import (
    RejectReport,
    RowReject,
    validate_row,
    validate_batch,
)


def _ts() -> datetime:
    return datetime(2024, 5, 2, 3, 45, 0, tzinfo=timezone.utc)  # 09:15 IST


# ---------------------------------------------------------------------------
# validate_row — valid rows
# ---------------------------------------------------------------------------

class TestValidRow:
    def test_normal_row_passes(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=1,
            raw_ticker="NIFTY.NSE",
            ts=_ts(),
            open_=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=1000.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is True
        assert rr.count == 0

    def test_zero_volume_allowed(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=1,
            raw_ticker="NIFTY.NSE",
            ts=_ts(),
            open_=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=0.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is True

    def test_high_equals_low_passes(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=1,
            raw_ticker="X",
            ts=_ts(),
            open_=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=10.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# validate_row — rejections
# ---------------------------------------------------------------------------

class TestRejections:
    def test_high_less_than_low_rejected(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=2,
            raw_ticker="NIFTY.NSE",
            ts=_ts(),
            open_=100.0,
            high=80.0,    # < low
            low=90.0,
            close=85.0,
            volume=500.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is False
        assert rr.count == 1
        assert "high" in rr.rejects[0].reason.lower()

    def test_negative_open_rejected(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=3,
            raw_ticker="NIFTY.NSE",
            ts=_ts(),
            open_=-5.0,
            high=10.0,
            low=5.0,
            close=8.0,
            volume=100.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is False
        assert any("open" in r.reason.lower() for r in rr.rejects)

    def test_negative_close_rejected(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=4,
            raw_ticker="X",
            ts=_ts(),
            open_=10.0,
            high=15.0,
            low=8.0,
            close=-1.0,
            volume=100.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is False

    def test_negative_volume_rejected(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=5,
            raw_ticker="X",
            ts=_ts(),
            open_=10.0,
            high=15.0,
            low=8.0,
            close=12.0,
            volume=-100.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is False
        assert "volume" in rr.rejects[0].reason.lower()

    def test_multiple_errors_combined(self):
        rr = RejectReport()
        ok = validate_row(
            row_index=6,
            raw_ticker="X",
            ts=_ts(),
            open_=-5.0,
            high=1.0,
            low=10.0,   # low > high
            close=-1.0,
            volume=0.0,
            open_interest=None,
            reject_report=rr,
        )
        assert ok is False
        reason = rr.rejects[0].reason
        # Should have at least two errors
        assert ";" in reason


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_strict_raises_on_invalid(self):
        with pytest.raises(ValueError, match="failed validation"):
            validate_row(
                row_index=1,
                raw_ticker="X",
                ts=_ts(),
                open_=10.0,
                high=5.0,   # < low
                low=8.0,
                close=7.0,
                volume=100.0,
                open_interest=None,
                strict=True,
            )

    def test_strict_ok_for_valid(self):
        # Should not raise
        ok = validate_row(
            row_index=1,
            raw_ticker="X",
            ts=_ts(),
            open_=10.0,
            high=15.0,
            low=8.0,
            close=12.0,
            volume=100.0,
            open_interest=None,
            strict=True,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# validate_batch
# ---------------------------------------------------------------------------

class TestValidateBatch:
    def _row(self, idx: int, high: float = 110.0, low: float = 90.0) -> dict:
        return {
            "row_index": idx,
            "raw_ticker": "NIFTY.NSE",
            "ts": _ts(),
            "open": 100.0,
            "high": high,
            "low": low,
            "close": 105.0,
            "volume": 1000,
            "open_interest": None,
        }

    def test_all_valid(self):
        rows = [self._row(i) for i in range(5)]
        rr = RejectReport()
        valid = validate_batch(rows, reject_report=rr)
        assert len(valid) == 5
        assert rr.count == 0

    def test_some_invalid(self):
        rows = [
            self._row(0),
            self._row(1, high=80.0, low=90.0),  # invalid: high < low
            self._row(2),
        ]
        rr = RejectReport()
        valid = validate_batch(rows, reject_report=rr)
        assert len(valid) == 2
        assert rr.count == 1

    def test_reject_report_summary(self):
        rows = [self._row(i, high=50.0, low=90.0) for i in range(3)]
        rr = RejectReport()
        validate_batch(rows, reject_report=rr)
        summary = rr.summary()
        assert "3" in summary
        assert not rr.is_empty()
