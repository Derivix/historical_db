"""
Option selection — pluggable framework + OFFSET implementation.

Selection methods compute, for each (underlying, expiry, timestamp), which
strikes are labelled ATM, ATM+1, ATM-1, etc. based on the current spot price.

The base class `SelectionMethod` defines the interface.  Methods are registered
by name in `METHOD_REGISTRY`.

OFFSET method
-------------
Params: {"range": N}
For each (underlying, expiry, ts):
  1. Spot = close of the INDEX/EQ instrument at ts
  2. ATM = strike with min abs(strike - spot) among listed strikes for expiry
  3. For offset in -N..+N, walk the sorted strike list from ATM position
  4. Write CE + PE rows for each offset label
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.db.connection import get_connection
from app.db import dao

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

@dataclass
class SelectionRow:
    """One row to write to option_selection."""
    underlying_id: int
    expiry: date
    ts: datetime
    rule_id: int
    label: str            # e.g. "ATM", "ATM+1", "ATM-1"
    option_type: str      # CE | PE
    strike: Decimal
    instrument_id: int | None
    spot: Decimal | None
    premium: Decimal | None
    meta: dict[str, Any] | None = None


class SelectionMethod(ABC):
    """Pluggable interface for option selection methods."""

    name: str = ""  # override in subclass

    @abstractmethod
    def compute(
        self,
        conn: Any,
        underlying_id: int,
        rule_id: int,
        params: dict[str, Any],
        timestamps: list[datetime],
    ) -> list[SelectionRow]:
        """
        Compute selection rows for the given underlying and timestamps.

        Parameters
        ----------
        conn:
            Active psycopg connection.
        underlying_id:
            ID of the underlying to compute selections for.
        rule_id:
            Selection rule ID (stored in option_selection).
        params:
            Method-specific parameters from selection_rule.params.
        timestamps:
            Sorted list of UTC timestamps to compute selections at.
        """


# ---------------------------------------------------------------------------
# METHOD_REGISTRY
# ---------------------------------------------------------------------------

METHOD_REGISTRY: dict[str, type[SelectionMethod]] = {}


def register_method(cls: type[SelectionMethod]) -> type[SelectionMethod]:
    METHOD_REGISTRY[cls.name] = cls
    return cls


# ---------------------------------------------------------------------------
# OFFSET implementation
# ---------------------------------------------------------------------------

@register_method
class OffsetMethod(SelectionMethod):
    """
    OFFSET selection: label strikes relative to ATM by position in the
    sorted listed-strike list (not arithmetic multiples of strike_step).
    """

    name = "OFFSET"

    def compute(
        self,
        conn: Any,
        underlying_id: int,
        rule_id: int,
        params: dict[str, Any],
        timestamps: list[datetime],
    ) -> list[SelectionRow]:
        n_offsets = int(params.get("range", 2))

        # 1. Find spot instrument (INDEX or EQ) for this underlying
        spot_instrument_id = _get_spot_instrument_id(conn, underlying_id)
        if spot_instrument_id is None:
            logger.warning("offset_no_spot_instrument", underlying_id=underlying_id)
            return []

        # 2. Fetch all option strikes per expiry for this underlying
        strike_map = _get_listed_strikes(conn, underlying_id)
        if not strike_map:
            logger.warning("offset_no_listed_strikes", underlying_id=underlying_id)
            return []

        # 3. Fetch spot prices at all relevant timestamps
        spot_prices = _get_spot_prices(conn, spot_instrument_id, timestamps)

        # 4. Fetch instrument_ids for option lookup
        option_instrument_map = _get_option_instrument_ids(conn, underlying_id)

        # 5. Fetch premiums (close price of each option instrument at each ts)
        # We'll look up on demand per row

        rows: list[SelectionRow] = []

        for ts in timestamps:
            spot = spot_prices.get(ts)
            if spot is None:
                continue

            for expiry, strikes in strike_map.items():
                if not strikes:
                    continue

                # ATM = nearest listed strike by abs(strike - spot)
                sorted_strikes = sorted(strikes)
                atm_idx = min(
                    range(len(sorted_strikes)),
                    key=lambda i: abs(float(sorted_strikes[i]) - float(spot))
                )

                for offset in range(-n_offsets, n_offsets + 1):
                    idx = atm_idx + offset
                    # Clamp to ends of the list
                    idx = max(0, min(idx, len(sorted_strikes) - 1))
                    strike = sorted_strikes[idx]

                    label = _offset_label(offset)

                    for opt_type in ("CE", "PE"):
                        ikey = (expiry, strike, opt_type)
                        instrument_id = option_instrument_map.get(ikey)
                        premium = _get_premium(conn, instrument_id, ts) if instrument_id else None

                        rows.append(SelectionRow(
                            underlying_id=underlying_id,
                            expiry=expiry,
                            ts=ts,
                            rule_id=rule_id,
                            label=label,
                            option_type=opt_type,
                            strike=strike,
                            instrument_id=instrument_id,
                            spot=spot,
                            premium=premium,
                            meta={"offset": offset, "atm_strike": float(sorted_strikes[atm_idx])},
                        ))

        return rows


# ---------------------------------------------------------------------------
# Helpers for OFFSET
# ---------------------------------------------------------------------------

def _get_spot_instrument_id(conn: Any, underlying_id: int) -> int | None:
    """
    Return the instrument_id to use as spot reference for this underlying.

    Priority:
    1. INDEX or EQ instrument (real cash/spot data)
    2. Front-month continuous future (continuous_rank=1, e.g. NIFTY-I) as proxy
       when no real spot is available in the data source.
    """
    row = conn.execute(
        """
        SELECT instrument_id FROM instrument
        WHERE underlying_id = %s
          AND instrument_type IN ('INDEX', 'EQ')
          AND expiry IS NULL
        ORDER BY instrument_type
        LIMIT 1
        """,
        (underlying_id,),
    ).fetchone()
    if row:
        return row[0]

    # Fallback: use front-month continuous future price as spot proxy
    row = conn.execute(
        """
        SELECT instrument_id FROM instrument
        WHERE underlying_id = %s
          AND instrument_type = 'FUT'
          AND continuous_rank = 1
        LIMIT 1
        """,
        (underlying_id,),
    ).fetchone()
    if row:
        logger.info("spot_fallback_to_continuous_fut", underlying_id=underlying_id)
    return row[0] if row else None


def _get_listed_strikes(
    conn: Any,
    underlying_id: int,
) -> dict[date, list[Decimal]]:
    """Return {expiry: sorted list of unique strikes} for all option instruments."""
    rows = conn.execute(
        """
        SELECT DISTINCT expiry, strike
        FROM instrument
        WHERE underlying_id = %s
          AND instrument_type IN ('CE', 'PE')
          AND expiry IS NOT NULL
          AND strike IS NOT NULL
        ORDER BY expiry, strike
        """,
        (underlying_id,),
    ).fetchall()

    result: dict[date, list[Decimal]] = {}
    for expiry, strike in rows:
        if expiry not in result:
            result[expiry] = []
        result[expiry].append(Decimal(str(strike)))
    return result


def _get_spot_prices(
    conn: Any,
    spot_instrument_id: int,
    timestamps: list[datetime],
) -> dict[datetime, Decimal]:
    """Return {ts: close} for the spot instrument at the given timestamps."""
    if not timestamps:
        return {}

    placeholders = ",".join(["%s"] * len(timestamps))
    rows = conn.execute(
        f"""
        SELECT ts, close FROM ohlcv
        WHERE instrument_id = %s
          AND ts IN ({placeholders})
        """,
        [spot_instrument_id, *timestamps],
    ).fetchall()
    return {r[0]: Decimal(str(r[1])) for r in rows}


def _get_option_instrument_ids(
    conn: Any,
    underlying_id: int,
) -> dict[tuple[date, Decimal, str], int]:
    """Return {(expiry, strike, option_type): instrument_id} for all options."""
    rows = conn.execute(
        """
        SELECT expiry, strike, option_type, instrument_id
        FROM instrument
        WHERE underlying_id = %s
          AND instrument_type IN ('CE', 'PE')
          AND expiry IS NOT NULL
          AND strike IS NOT NULL
          AND option_type IS NOT NULL
        """,
        (underlying_id,),
    ).fetchall()
    return {
        (r[0], Decimal(str(r[1])), r[2]): r[3]
        for r in rows
    }


def _get_premium(conn: Any, instrument_id: int, ts: datetime) -> Decimal | None:
    """Return the close price of an option at a given timestamp."""
    row = conn.execute(
        "SELECT close FROM ohlcv WHERE instrument_id = %s AND ts = %s",
        (instrument_id, ts),
    ).fetchone()
    return Decimal(str(row[0])) if row else None


def _offset_label(offset: int) -> str:
    if offset == 0:
        return "ATM"
    sign = "+" if offset > 0 else ""
    return f"ATM{sign}{offset}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_selections(
    method: str = "OFFSET",
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    """
    Compute and store option selections for all active rules matching `method`.

    Returns total rows written to option_selection.
    """
    total = 0
    method_cls = METHOD_REGISTRY.get(method)
    if method_cls is None:
        raise ValueError(f"Unknown selection method {method!r}. Available: {list(METHOD_REGISTRY)}")

    method_obj = method_cls()

    with get_connection() as conn:
        rules = dao.fetch_active_selection_rules(conn)
        rules = [r for r in rules if r["method"] == method]

        if not rules:
            logger.info("no_active_rules", method=method)
            return 0

        for rule in rules:
            underlying_id = rule["underlying_id"]
            rule_id = rule["rule_id"]
            params = rule["params"] if isinstance(rule["params"], dict) else json.loads(rule["params"])

            # Get all timestamps for this underlying (from ohlcv)
            params_q: list[Any] = [underlying_id]
            where_extra = ""
            if start_date:
                where_extra += " AND o.ts >= %s"
                params_q.append(start_date)
            if end_date:
                where_extra += " AND o.ts < %s"
                params_q.append(end_date)

            ts_rows = conn.execute(
                f"""
                SELECT DISTINCT o.ts FROM ohlcv o
                JOIN instrument i ON o.instrument_id = i.instrument_id
                WHERE i.underlying_id = %s {where_extra}
                ORDER BY o.ts
                """,
                params_q,
            ).fetchall()
            timestamps = [r[0] for r in ts_rows]

            if not timestamps:
                continue

            selection_rows = method_obj.compute(
                conn, underlying_id, rule_id, params, timestamps
            )

            if selection_rows:
                dicts = [
                    {
                        "underlying_id": r.underlying_id,
                        "expiry": r.expiry,
                        "ts": r.ts,
                        "rule_id": r.rule_id,
                        "label": r.label,
                        "option_type": r.option_type,
                        "strike": float(r.strike),
                        "instrument_id": r.instrument_id,
                        "spot": float(r.spot) if r.spot is not None else None,
                        "premium": float(r.premium) if r.premium is not None else None,
                        "meta": r.meta,
                    }
                    for r in selection_rows
                ]
                written = dao.copy_option_selection_rows(conn, dicts)
                conn.commit()
                total += written
                logger.info(
                    "selections_written",
                    method=method,
                    rule_id=rule_id,
                    underlying_id=underlying_id,
                    rows=written,
                )

    logger.info("run_selections_done", method=method, total=total)
    return total
