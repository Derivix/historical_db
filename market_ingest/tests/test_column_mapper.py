"""
Tests for app.ingest.column_mapper
"""
from __future__ import annotations

import pytest

from app.config import ColumnMapProfile
from app.ingest.column_mapper import (
    ColumnMapError,
    MappingResult,
    map_columns,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_profile(granularity: str = "intraday", extra: dict | None = None) -> ColumnMapProfile:
    base = {
        "ticker": ["ticker", "symbol"],
        "date": ["date", "trade_date"],
        "time": ["time", "trade_time"],
        "open": ["open", "open_price", "o"],
        "high": ["high", "high_price", "h"],
        "low": ["low", "low_price", "l"],
        "close": ["close", "ltp", "c"],
        "volume": ["volume", "vol"],
        "open_interest": ["open_interest", "oi", "open interest"],
    }
    if extra:
        base.update(extra)
    return ColumnMapProfile(
        granularity=granularity,
        timezone="Asia/Kolkata",
        datetime_format="%m/%d/%Y %H:%M:%S",
        column_map=base,
    )


SAMPLE_HEADERS = ["Ticker", "Date", "Time", "Open", "High", "Low", "Close", "Volume", "Open Interest"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_exact_match(self):
        profile = _make_profile()
        result = map_columns(SAMPLE_HEADERS, profile)
        assert result.get("ticker") == "Ticker"
        assert result.get("date") == "Date"
        assert result.get("time") == "Time"
        assert result.get("open") == "Open"
        assert result.get("high") == "High"
        assert result.get("low") == "Low"
        assert result.get("close") == "Close"
        assert result.get("volume") == "Volume"
        assert result.get("open_interest") == "Open Interest"

    def test_alias_match(self):
        profile = _make_profile()
        headers = ["symbol", "trade_date", "trade_time", "open_price", "high_price", "low_price", "ltp", "vol"]
        result = map_columns(headers, profile)
        assert result.get("ticker") == "symbol"
        assert result.get("date") == "trade_date"
        assert result.get("open") == "open_price"
        assert result.get("close") == "ltp"

    def test_case_insensitive(self):
        profile = _make_profile()
        headers = ["TICKER", "DATE", "TIME", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
        result = map_columns(headers, profile)
        assert result.get("ticker") == "TICKER"

    def test_open_interest_optional_for_intraday(self):
        profile = _make_profile()
        # No OI column — should succeed (OI is optional)
        headers = ["Ticker", "Date", "Time", "Open", "High", "Low", "Close", "Volume"]
        result = map_columns(headers, profile)
        assert result.get("open_interest") is None

    def test_volume_optional_when_missing(self):
        profile = _make_profile()
        headers = ["Ticker", "Date", "Time", "Open", "High", "Low", "Close"]
        result = map_columns(headers, profile)
        assert result.get("volume") is None

    def test_require_method_raises_for_unmapped(self):
        profile = _make_profile()
        headers = ["Ticker", "Date", "Time", "Open", "High", "Low", "Close", "Volume"]
        result = map_columns(headers, profile)
        with pytest.raises(ColumnMapError):
            result.require("open_interest")

    def test_daily_granularity_no_time_required(self):
        profile = _make_profile(granularity="daily")
        headers = ["Ticker", "Date", "Open", "High", "Low", "Close", "Volume"]
        # time not present — should be fine for daily
        result = map_columns(headers, profile)
        assert result.get("time") is None

    def test_gfd_profile_open_interest_with_space(self):
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
        headers = ["Ticker", "Date", "Time", "Open", "High", "Low", "Close", "Volume", "Open Interest"]
        result = map_columns(headers, profile)
        assert result.get("open_interest") == "Open Interest"


# ---------------------------------------------------------------------------
# Missing required field
# ---------------------------------------------------------------------------

class TestMissingRequiredField:
    def test_missing_ticker(self):
        profile = _make_profile()
        headers = ["Date", "Time", "Open", "High", "Low", "Close", "Volume"]
        with pytest.raises(ColumnMapError, match="ticker"):
            map_columns(headers, profile)

    def test_missing_close(self):
        profile = _make_profile()
        headers = ["Ticker", "Date", "Time", "Open", "High", "Low", "Volume"]
        with pytest.raises(ColumnMapError, match="close"):
            map_columns(headers, profile)

    def test_missing_time_for_intraday(self):
        profile = _make_profile(granularity="intraday")
        headers = ["Ticker", "Date", "Open", "High", "Low", "Close", "Volume"]
        with pytest.raises(ColumnMapError, match="time"):
            map_columns(headers, profile)


# ---------------------------------------------------------------------------
# Ambiguity detection
# ---------------------------------------------------------------------------

class TestAmbiguity:
    def test_one_header_matches_two_canonicals_via_profile(self):
        """
        If the profile incorrectly lists the same alias under two canonical fields,
        ColumnMapError should be raised at mapping time.
        """
        profile = ColumnMapProfile(
            granularity="intraday",
            timezone="Asia/Kolkata",
            datetime_format="%m/%d/%Y %H:%M:%S",
            column_map={
                "ticker": ["ticker"],
                "date": ["date"],
                "time": ["time"],
                "open": ["open", "price"],   # "price" also appears below → ambiguous profile
                "close": ["close", "price"],
                "high": ["high"],
                "low": ["low"],
                "volume": ["volume"],
            }
        )
        with pytest.raises(ColumnMapError, match="appears in both"):
            map_columns(["ticker", "date", "time", "open", "high", "low", "close", "volume", "price"], profile)

    def test_two_source_headers_map_to_same_canonical(self):
        """
        If source file has both 'open' and 'open_price' and both alias to 'open',
        ColumnMapError should be raised.
        """
        profile = _make_profile()
        headers = ["Ticker", "Date", "Time", "Open", "open_price", "High", "Low", "Close", "Volume"]
        with pytest.raises(ColumnMapError, match="matched by two source headers"):
            map_columns(headers, profile)


# ---------------------------------------------------------------------------
# MappingResult helpers
# ---------------------------------------------------------------------------

class TestMappingResult:
    def test_get_returns_none_for_missing(self):
        result = MappingResult()
        assert result.get("open_interest") is None

    def test_require_raises_for_missing(self):
        result = MappingResult()
        with pytest.raises(ColumnMapError, match="Required canonical field"):
            result.require("open_interest")
