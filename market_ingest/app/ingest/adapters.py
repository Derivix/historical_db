"""Normalise provider-specific market-data rows into database instruments."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import ColumnMapProfile
from app.ingest.column_mapper import MappingResult
from app.ingest.ticker_parser import ParsedTicker, TickerParseError


def resolve_instrument(
    row: dict[str, Any],
    mapping: MappingResult,
    profile: ColumnMapProfile,
    ts_utc: datetime,
) -> tuple[str, ParsedTicker]:
    """Return the stable source identifier and structured database instrument.

    Profiles without an adapter continue to use the existing ticker parser.
    """
    if profile.adapter is None:
        from app.ingest.ticker_parser import parse_ticker

        raw_ticker = _value(row, mapping.require("ticker"))
        return raw_ticker, parse_ticker(raw_ticker)

    if profile.adapter.name != "option_snapshot":
        raise TickerParseError(f"Unknown source adapter {profile.adapter.name!r}")

    adapter = profile.adapter
    if not adapter.underlying_symbol:
        raise TickerParseError("option_snapshot adapter requires underlying_symbol")

    raw_ticker = _value(row, mapping.require("ticker"))
    if not raw_ticker:
        raise TickerParseError("Empty instrument_key")

    option_type = _value(row, mapping.require("option_type")).upper()
    if option_type not in {"CE", "PE"}:
        raise TickerParseError(f"Invalid option type {option_type!r}")

    try:
        strike = float(_value(row, mapping.require("strike")))
    except ValueError as exc:
        raise TickerParseError("Invalid option strike") from exc
    if strike <= 0:
        raise TickerParseError(f"Invalid option strike {strike!r}")

    try:
        tte_years = Decimal(_value(row, mapping.require("time_to_expiry")))
        days_to_expiry = int((tte_years * Decimal(str(adapter.expiry_days_per_year))).to_integral_value())
    except (InvalidOperation, ValueError) as exc:
        raise TickerParseError("Invalid time to expiry") from exc
    if days_to_expiry < 0:
        raise TickerParseError(f"Negative time to expiry {tte_years}")

    return raw_ticker, ParsedTicker(
        raw_ticker=raw_ticker,
        symbol=adapter.underlying_symbol.upper(),
        segment=adapter.exchange.upper(),
        instrument_type=option_type,
        expiry=ts_utc.date() + timedelta(days=days_to_expiry),
        strike=strike,
        option_type=option_type,
    )


def source_features(row: dict[str, Any], mapping: MappingResult) -> dict[str, float | None]:
    """Extract optional provider Greeks for the feature table."""
    return {field: _float_or_none(row.get(mapping.get(field))) for field in ("iv", "delta", "gamma", "theta", "vega")}


def _value(row: dict[str, Any], column: str) -> str:
    value = row.get(column)
    return "" if value is None else str(value).strip()


def _float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
