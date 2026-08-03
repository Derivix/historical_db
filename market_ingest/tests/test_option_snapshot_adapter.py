from __future__ import annotations

from datetime import datetime, timezone

from app.config import load_profile
from app.ingest.adapters import resolve_instrument, source_features
from app.ingest.column_mapper import map_columns


HEADERS = [
    "Date", "Time", "strike", "opt_type", "open", "high", "low", "close",
    "volume", "oi", "spot", "tte", "iv", "delta", "gamma", "theta", "vega", "instrument_key",
]


def test_option_snapshot_profile_maps_updated_csv_header() -> None:
    profile = load_profile("option_snapshot")
    mapping = map_columns(HEADERS, profile)
    assert mapping.get("ticker") == "instrument_key"
    assert mapping.get("date") == "Date"
    assert mapping.get("time") == "Time"


def test_option_snapshot_adapter_creates_database_option() -> None:
    profile = load_profile("option_snapshot")
    mapping = map_columns(HEADERS, profile)
    row = dict(zip(HEADERS, [
        "2026-04-06", "03:45:00+00", "20200", "PE", "86", "86.85", "71.95", "81.15",
        "19890", "85345", "22690", "0.060987443", "0.36453283", "-0.083750054", "7.53E-05", "-6.707624", "8.623054", "NSE_FO|71808",
    ]))
    raw_ticker, instrument = resolve_instrument(row, mapping, profile, datetime(2026, 4, 6, 3, 45, tzinfo=timezone.utc))

    assert raw_ticker == "NSE_FO|71808"
    assert instrument.symbol == "NIFTY"
    assert instrument.segment == "NFO"
    assert instrument.instrument_type == instrument.option_type == "PE"
    assert instrument.strike == 20200.0
    assert instrument.expiry.isoformat() == "2026-04-28"
    assert source_features(row, mapping)["delta"] == -0.083750054
