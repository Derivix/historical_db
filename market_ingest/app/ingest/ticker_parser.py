"""
Ticker string → structured instrument.

Supported formats
-----------------
Options  : AARTIIND27JUN24700CE.NFO
           NIFTY27JUN2423500CE.NFO
Futures  : NIFTYFUT27JUN24.NFO
           RELIANCE FUT27JUN24.NFO  (rare but handled)
Spot/EQ  : RELIANCE.NSE
Index    : NIFTY.NSE  (handled as INDEX when underlying kind=INDEX, EQ otherwise)

Expiry format: DDMMMYY  e.g. 27JUN24  → 2024-06-27
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class TickerParseError(ValueError):
    """Raised when a ticker string cannot be unambiguously parsed."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedTicker:
    raw_ticker: str
    symbol: str            # underlying symbol, e.g. AARTIIND, NIFTY
    segment: str           # NFO, NSE, BSE, MCX …
    instrument_type: str   # INDEX, EQ, FUT, CE, PE
    expiry: date | None
    strike: float | None
    option_type: str | None   # CE | PE | None
    continuous_rank: int | None = None  # 1/2/3 for back-adjusted -I/-II/-III futures; None otherwise


# ---------------------------------------------------------------------------
# Month lookup
# ---------------------------------------------------------------------------

_MONTH_MAP: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# NSE indices (not exhaustive; used to set kind=INDEX for spot lines)
_INDEX_SYMBOLS = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYMIDCAP50",
    "SENSEX", "BANKEX", "NIFTY50", "NIFTY100", "NIFTYNXT50",
})


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Options: <SYMBOL><DDMMMYY><STRIKE>[CE|PE].<SEGMENT>
_RE_OPTION = re.compile(
    r"^(?P<symbol>[A-Z0-9&-]+?)(?P<expiry>\d{2}[A-Z]{3}\d{2})(?P<strike>\d+(?:\.\d+)?)(?P<opt_type>CE|PE)\.(?P<segment>\w+)$"
)

# Futures: <SYMBOL>FUT<DDMMMYY>.<SEGMENT>
_RE_FUTURE = re.compile(
    r"^(?P<symbol>[A-Z0-9&-]+?)FUT(?P<expiry>\d{2}[A-Z]{3}\d{2})\.(?P<segment>\w+)$"
)

# Continuous (back-adjusted) futures: <SYMBOL>-O.<SEGMENT>, -I, -II, -III
# Zerodha format: NIFTY-O.NFO (front month proxy), NIFTY-I.NFO, NIFTY-II.NFO, NIFTY-III.NFO
# Symbol may contain digits or hyphens, e.g. 360ONE-I.NFO or BAJAJ-AUTO-I.NFO
_RE_CONTINUOUS_FUT = re.compile(
    r"^(?P<symbol>[A-Z0-9&-]+)-(?P<rank>O|I{1,3})\.(?P<segment>\w+)$"
)

_CONTINUOUS_RANK: dict[str, int] = {"O": 0, "I": 1, "II": 2, "III": 3}

# Spot / Index EQ: <SYMBOL>.<SEGMENT>
_RE_SPOT = re.compile(
    r"^(?P<symbol>[A-Z0-9&-]+)\.(?P<segment>\w+)$"
)


# ---------------------------------------------------------------------------
# Helper: parse expiry string DDMMMYY → date
# ---------------------------------------------------------------------------

def _parse_expiry(expiry_str: str) -> date:
    """Parse DDMMMYY (e.g. '27JUN24') into a date object."""
    if len(expiry_str) != 7:
        raise TickerParseError(f"Expiry string {expiry_str!r} has unexpected length (expected 7)")

    day_str  = expiry_str[:2]
    mon_str  = expiry_str[2:5].upper()
    year_str = expiry_str[5:]

    try:
        day = int(day_str)
    except ValueError:
        raise TickerParseError(f"Non-numeric day in expiry {expiry_str!r}")

    month = _MONTH_MAP.get(mon_str)
    if month is None:
        raise TickerParseError(f"Unknown month abbreviation {mon_str!r} in expiry {expiry_str!r}")

    try:
        year = 2000 + int(year_str)
    except ValueError:
        raise TickerParseError(f"Non-numeric year in expiry {expiry_str!r}")

    try:
        return date(year, month, day)
    except ValueError as exc:
        raise TickerParseError(f"Invalid date in expiry {expiry_str!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_ticker(raw_ticker: str) -> ParsedTicker:
    """
    Parse a raw ticker string into a ParsedTicker.

    Raises TickerParseError for anything that cannot be unambiguously parsed.
    Never returns a guess.
    """
    ticker = raw_ticker.strip()
    if not ticker:
        raise TickerParseError("Empty ticker string")

    # --- Try option ---
    m = _RE_OPTION.match(ticker)
    if m:
        symbol   = m.group("symbol")
        segment  = m.group("segment")
        opt_type = m.group("opt_type")
        try:
            expiry = _parse_expiry(m.group("expiry"))
        except TickerParseError as exc:
            raise TickerParseError(f"Cannot parse option ticker {ticker!r}: {exc}") from exc
        try:
            strike = float(m.group("strike"))
        except ValueError:
            raise TickerParseError(f"Cannot parse strike in {ticker!r}")

        return ParsedTicker(
            raw_ticker=raw_ticker,
            symbol=symbol,
            segment=segment,
            instrument_type=opt_type,  # CE or PE
            expiry=expiry,
            strike=strike,
            option_type=opt_type,
        )

    # --- Try future ---
    m = _RE_FUTURE.match(ticker)
    if m:
        symbol  = m.group("symbol")
        segment = m.group("segment")
        try:
            expiry = _parse_expiry(m.group("expiry"))
        except TickerParseError as exc:
            raise TickerParseError(f"Cannot parse future ticker {ticker!r}: {exc}") from exc

        return ParsedTicker(
            raw_ticker=raw_ticker,
            symbol=symbol,
            segment=segment,
            instrument_type="FUT",
            expiry=expiry,
            strike=None,
            option_type=None,
        )

    # --- Try continuous (back-adjusted) future: SYMBOL-I.NFO, SYMBOL-II.NFO, SYMBOL-III.NFO ---
    m = _RE_CONTINUOUS_FUT.match(ticker)
    if m:
        symbol  = m.group("symbol")
        segment = m.group("segment")
        rank    = _CONTINUOUS_RANK[m.group("rank")]

        return ParsedTicker(
            raw_ticker=raw_ticker,
            symbol=symbol,
            segment=segment,
            instrument_type="FUT",
            expiry=None,
            strike=None,
            option_type=None,
            continuous_rank=rank,
        )

    # --- Try spot / EQ / INDEX ---
    if "." in ticker:
        stem, _segment = ticker.rsplit(".", 1)
        if re.search(r"\d{2}[A-Z]{3}\d{2}[-]?\d*(?:CE|PE)$", stem, re.IGNORECASE):
            raise TickerParseError(
                f"Cannot parse option ticker {ticker!r}: invalid option structure"
            )

    m = _RE_SPOT.match(ticker)
    if m:
        symbol  = m.group("symbol")
        segment = m.group("segment")
        kind    = "INDEX" if symbol in _INDEX_SYMBOLS else "EQ"

        return ParsedTicker(
            raw_ticker=raw_ticker,
            symbol=symbol,
            segment=segment,
            instrument_type=kind,
            expiry=None,
            strike=None,
            option_type=None,
        )

    raise TickerParseError(
        f"Cannot parse ticker {ticker!r}: does not match option, future, or spot patterns"
    )


def infer_underlying_kind(symbol: str, instrument_type: str) -> str:
    """
    Infer whether an underlying is an INDEX or STOCK.
    Options/futures from NFO segment on known indices → INDEX, else STOCK.
    Spot line explicitly determines kind via instrument_type.
    """
    if instrument_type in ("INDEX",):
        return "INDEX"
    if symbol in _INDEX_SYMBOLS:
        return "INDEX"
    return "STOCK"
