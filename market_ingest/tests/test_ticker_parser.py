"""
Tests for app.ingest.ticker_parser
"""
from __future__ import annotations

from datetime import date

import pytest

from app.ingest.ticker_parser import (
    ParsedTicker,
    TickerParseError,
    parse_ticker,
    _parse_expiry,
)


# ---------------------------------------------------------------------------
# Expiry parsing
# ---------------------------------------------------------------------------

class TestParseExpiry:
    def test_standard(self):
        assert _parse_expiry("27JUN24") == date(2024, 6, 27)

    def test_jan(self):
        assert _parse_expiry("01JAN25") == date(2025, 1, 1)

    def test_dec(self):
        assert _parse_expiry("31DEC23") == date(2023, 12, 31)

    def test_bad_month(self):
        with pytest.raises(TickerParseError, match="Unknown month"):
            _parse_expiry("27XXX24")

    def test_bad_day(self):
        with pytest.raises(TickerParseError, match="Invalid date"):
            _parse_expiry("32JAN24")

    def test_too_short(self):
        with pytest.raises(TickerParseError, match="unexpected length"):
            _parse_expiry("27JN24")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

class TestOptionParsing:
    def test_aartiind_ce(self):
        p = parse_ticker("AARTIIND27JUN24700CE.NFO")
        assert p.symbol == "AARTIIND"
        assert p.expiry == date(2024, 6, 27)
        assert p.strike == 700.0
        assert p.option_type == "CE"
        assert p.instrument_type == "CE"
        assert p.segment == "NFO"

    def test_aartiind_pe(self):
        p = parse_ticker("AARTIIND30MAY24600PE.NFO")
        assert p.symbol == "AARTIIND"
        assert p.expiry == date(2024, 5, 30)
        assert p.strike == 600.0
        assert p.option_type == "PE"
        assert p.instrument_type == "PE"

    def test_nifty_ce(self):
        p = parse_ticker("NIFTY27JUN2423500CE.NFO")
        assert p.symbol == "NIFTY"
        assert p.expiry == date(2024, 6, 27)
        assert p.strike == 23500.0
        assert p.option_type == "CE"

    def test_nifty_pe(self):
        p = parse_ticker("NIFTY27JUN2423400PE.NFO")
        assert p.symbol == "NIFTY"
        assert p.strike == 23400.0
        assert p.option_type == "PE"

    def test_decimal_strike(self):
        p = parse_ticker("NIFTY27JUN2423500.5CE.NFO")
        assert p.strike == 23500.5

    def test_bse_segment(self):
        p = parse_ticker("SENSEX27JUN2472000CE.BSE")
        assert p.symbol == "SENSEX"
        assert p.segment == "BSE"
        assert p.option_type == "CE"


# ---------------------------------------------------------------------------
# Futures
# ---------------------------------------------------------------------------

class TestFutureParsing:
    def test_nifty_fut(self):
        p = parse_ticker("NIFTYFUT27JUN24.NFO")
        assert p.symbol == "NIFTY"
        assert p.instrument_type == "FUT"
        assert p.expiry == date(2024, 6, 27)
        assert p.strike is None
        assert p.option_type is None
        assert p.continuous_rank is None

    def test_reliance_fut(self):
        p = parse_ticker("RELIANCEFUT30MAY24.NFO")
        assert p.symbol == "RELIANCE"
        assert p.instrument_type == "FUT"
        assert p.expiry == date(2024, 5, 30)
        assert p.continuous_rank is None


# ---------------------------------------------------------------------------
# Continuous (back-adjusted) futures  — Zerodha -I/-II/-III format
# ---------------------------------------------------------------------------

class TestContinuousFutureParsing:
    def test_nifty_front_month(self):
        p = parse_ticker("NIFTY-I.NFO")
        assert p.symbol == "NIFTY"
        assert p.instrument_type == "FUT"
        assert p.expiry is None
        assert p.strike is None
        assert p.option_type is None
        assert p.continuous_rank == 1
        assert p.segment == "NFO"

    def test_nifty_second_month(self):
        p = parse_ticker("NIFTY-II.NFO")
        assert p.symbol == "NIFTY"
        assert p.continuous_rank == 2

    def test_nifty_third_month(self):
        p = parse_ticker("NIFTY-III.NFO")
        assert p.symbol == "NIFTY"
        assert p.continuous_rank == 3

    def test_banknifty_front_month(self):
        p = parse_ticker("BANKNIFTY-I.NFO")
        assert p.symbol == "BANKNIFTY"
        assert p.instrument_type == "FUT"
        assert p.continuous_rank == 1

    def test_symbol_with_digits(self):
        # 360ONE-I.NFO — symbol may start with a digit
        p = parse_ticker("360ONE-I.NFO")
        assert p.symbol == "360ONE"
        assert p.instrument_type == "FUT"
        assert p.continuous_rank == 1
        assert p.raw_ticker == "360ONE-I.NFO"

    def test_nifty_o_rank_zero(self):
        p = parse_ticker("NIFTY-O.NFO")
        assert p.symbol == "NIFTY"
        assert p.instrument_type == "FUT"
        assert p.continuous_rank == 0
        assert p.segment == "NFO"

    def test_all_indices_front_month(self):
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            p = parse_ticker(f"{sym}-I.NFO")
            assert p.symbol == sym
            assert p.continuous_rank == 1


# ---------------------------------------------------------------------------
# Spot / EQ / INDEX
# ---------------------------------------------------------------------------

class TestSpotParsing:
    def test_nifty_index(self):
        p = parse_ticker("NIFTY.NSE")
        assert p.symbol == "NIFTY"
        assert p.instrument_type == "INDEX"
        assert p.segment == "NSE"
        assert p.expiry is None
        assert p.strike is None

    def test_banknifty_index(self):
        p = parse_ticker("BANKNIFTY.NSE")
        assert p.instrument_type == "INDEX"

    def test_reliance_eq(self):
        p = parse_ticker("RELIANCE.NSE")
        assert p.symbol == "RELIANCE"
        assert p.instrument_type == "EQ"
        assert p.segment == "NSE"

    def test_aartiind_eq(self):
        p = parse_ticker("AARTIIND.NSE")
        assert p.instrument_type == "EQ"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestParseErrors:
    def test_empty_string(self):
        with pytest.raises(TickerParseError, match="Empty ticker"):
            parse_ticker("")

    def test_whitespace_only(self):
        with pytest.raises(TickerParseError, match="Empty ticker"):
            parse_ticker("   ")

    def test_gibberish(self):
        with pytest.raises(TickerParseError):
            parse_ticker("NOTAVALIDTICKER123!!!")

    def test_no_segment(self):
        with pytest.raises(TickerParseError):
            parse_ticker("NIFTY27JUN2423500CE")

    def test_bad_expiry_month_in_option(self):
        with pytest.raises(TickerParseError):
            parse_ticker("NIFTY27XXX2423500CE.NFO")

    def test_negative_strike_is_rejected(self):
        # Negative strikes don't match the regex (no minus sign in pattern)
        with pytest.raises(TickerParseError):
            parse_ticker("NIFTY27JUN24-100CE.NFO")

    def test_raw_ticker_preserved(self):
        raw = "AARTIIND27JUN24700CE.NFO"
        p = parse_ticker(raw)
        assert p.raw_ticker == raw
