"""
Data Access Object — upserts, bulk COPY loaders, and query helpers.

All writes use psycopg v3's copy() method with COPY FROM STDIN (FORMAT CSV)
for maximum throughput.  Upserts use ON CONFLICT DO NOTHING / DO UPDATE to
ensure idempotency.
"""
from __future__ import annotations

import io
import csv
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

import psycopg
import structlog

from app.db.connection import get_connection

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# underlying
# ---------------------------------------------------------------------------

def upsert_underlying(
    conn: psycopg.Connection,
    symbol: str,
    kind: str,
    exchange: str,
    strike_step: Decimal | None = None,
    lot_size: int | None = None,
) -> int:
    """
    Insert or return existing underlying_id.
    Returns the underlying_id (int).
    """
    row = conn.execute(
        """
        INSERT INTO underlying (symbol, kind, exchange, strike_step, lot_size)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol, kind) DO UPDATE
            SET exchange    = EXCLUDED.exchange,
                strike_step = COALESCE(EXCLUDED.strike_step, underlying.strike_step),
                lot_size    = COALESCE(EXCLUDED.lot_size,    underlying.lot_size)
        RETURNING underlying_id
        """,
        (symbol, kind, exchange, strike_step, lot_size),
    ).fetchone()
    return row[0]  # type: ignore[index]


# ---------------------------------------------------------------------------
# instrument
# ---------------------------------------------------------------------------

def upsert_instrument(
    conn: psycopg.Connection,
    raw_ticker: str,
    underlying_id: int,
    instrument_type: str,
    exchange: str,
    expiry: date | None = None,
    strike: Decimal | None = None,
    option_type: str | None = None,
    lot_size: int | None = None,
    continuous_rank: int | None = None,
) -> int:
    """
    Insert or return existing instrument_id.
    Uses raw_ticker as the conflict key (always unique per instrument).
    continuous_rank (1/2/3) distinguishes back-adjusted rolling futures
    (e.g. NIFTY-I, NIFTY-II, NIFTY-III) from each other and from dated futures.
    """
    row = conn.execute(
        """
        INSERT INTO instrument
            (raw_ticker, underlying_id, instrument_type, exchange,
             expiry, strike, option_type, lot_size, continuous_rank)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (raw_ticker) DO UPDATE
            SET underlying_id   = EXCLUDED.underlying_id,
                instrument_type = EXCLUDED.instrument_type,
                exchange        = EXCLUDED.exchange,
                expiry          = COALESCE(EXCLUDED.expiry,          instrument.expiry),
                strike          = COALESCE(EXCLUDED.strike,          instrument.strike),
                option_type     = COALESCE(EXCLUDED.option_type,     instrument.option_type),
                lot_size        = COALESCE(EXCLUDED.lot_size,        instrument.lot_size),
                continuous_rank = COALESCE(EXCLUDED.continuous_rank, instrument.continuous_rank)
        RETURNING instrument_id
        """,
        (raw_ticker, underlying_id, instrument_type, exchange,
         expiry, strike if strike is not None else None,
         option_type if option_type else None, lot_size, continuous_rank),
    ).fetchone()
    return row[0]  # type: ignore[index]


def get_instrument_id(conn: psycopg.Connection, raw_ticker: str) -> int | None:
    """Return instrument_id for a raw ticker, or None if not found."""
    row = conn.execute(
        "SELECT instrument_id FROM instrument WHERE raw_ticker = %s",
        (raw_ticker,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# ohlcv bulk COPY
# ---------------------------------------------------------------------------

def copy_ohlcv_rows(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    """
    Bulk-insert OHLCV rows via COPY FROM STDIN.

    Each dict must have keys:
        instrument_id, ts (UTC datetime), open, high, low, close, volume,
        open_interest (optional / None)

    Returns the number of rows accepted (conflicts are silently dropped).
    """
    if not rows:
        return 0

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for r in rows:
        writer.writerow([
            r["instrument_id"],
            _fmt_ts(r["ts"]),
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r["volume"],
            r.get("open_interest") if r.get("open_interest") is not None else "",
        ])

    buf.seek(0)
    csv_data = buf.getvalue()

    # COPY into a temp staging table then INSERT ... ON CONFLICT DO NOTHING
    # to honour the PK (idempotent).
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS ohlcv_stage (LIKE ohlcv INCLUDING ALL)
        ON COMMIT DROP
    """)
    conn.execute("TRUNCATE ohlcv_stage")

    with conn.cursor() as cur:
        with cur.copy(
            "COPY ohlcv_stage (instrument_id, ts, open, high, low, close, volume, open_interest) "
            "FROM STDIN (FORMAT CSV, NULL '')"
        ) as copy:
            copy.write(csv_data)

    result = conn.execute("""
        INSERT INTO ohlcv (instrument_id, ts, open, high, low, close, volume, open_interest)
        SELECT instrument_id, ts, open, high, low, close, volume, open_interest
        FROM ohlcv_stage
        ON CONFLICT (instrument_id, ts) DO NOTHING
    """)
    inserted = result.rowcount if result.rowcount >= 0 else 0
    logger.debug("ohlcv_copy_done", rows_in=len(rows), rows_inserted=inserted)
    return inserted


# ---------------------------------------------------------------------------
# feature bulk COPY
# ---------------------------------------------------------------------------

def copy_feature_rows(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    """
    Bulk-insert feature rows (VWAP etc.) via COPY + upsert.

    Each dict must have keys:
        instrument_id, ts, vwap (others optional / None).
    """
    if not rows:
        return 0

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for r in rows:
        writer.writerow([
            r["instrument_id"],
            _fmt_ts(r["ts"]),
            r.get("vwap") if r.get("vwap") is not None else "",
            r.get("iv")    if r.get("iv")    is not None else "",
            r.get("delta") if r.get("delta") is not None else "",
            r.get("gamma") if r.get("gamma") is not None else "",
            r.get("theta") if r.get("theta") is not None else "",
            r.get("vega")  if r.get("vega")  is not None else "",
        ])

    buf.seek(0)

    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS feature_stage (LIKE feature INCLUDING ALL)
        ON COMMIT DROP
    """)
    conn.execute("TRUNCATE feature_stage")

    with conn.cursor() as cur:
        with cur.copy(
            "COPY feature_stage (instrument_id, ts, vwap, iv, delta, gamma, theta, vega) "
            "FROM STDIN (FORMAT CSV, NULL '')"
        ) as copy:
            copy.write(buf.getvalue())

    result = conn.execute("""
        INSERT INTO feature (instrument_id, ts, vwap, iv, delta, gamma, theta, vega)
        SELECT instrument_id, ts, vwap, iv, delta, gamma, theta, vega
        FROM feature_stage
        ON CONFLICT (instrument_id, ts) DO UPDATE
            SET vwap  = COALESCE(EXCLUDED.vwap,  feature.vwap),
                iv    = COALESCE(EXCLUDED.iv,    feature.iv),
                delta = COALESCE(EXCLUDED.delta, feature.delta),
                gamma = COALESCE(EXCLUDED.gamma, feature.gamma),
                theta = COALESCE(EXCLUDED.theta, feature.theta),
                vega  = COALESCE(EXCLUDED.vega,  feature.vega)
    """)
    inserted = result.rowcount if result.rowcount >= 0 else 0
    logger.debug("feature_copy_done", rows_in=len(rows), rows_inserted=inserted)
    return inserted


# ---------------------------------------------------------------------------
# option_selection bulk COPY
# ---------------------------------------------------------------------------

def copy_option_selection_rows(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    """
    Bulk-insert option_selection rows.

    Each dict: underlying_id, expiry, ts, rule_id, label, option_type,
                strike, instrument_id (opt), spot (opt), premium (opt), meta (opt)
    """
    if not rows:
        return 0

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for r in rows:
        meta = r.get("meta")
        meta_str = "" if meta is None else str(meta).replace("'", '"')
        writer.writerow([
            r["underlying_id"],
            r["expiry"],
            _fmt_ts(r["ts"]),
            r["rule_id"],
            r["label"],
            r["option_type"],
            r["strike"],
            r.get("instrument_id") if r.get("instrument_id") is not None else "",
            r.get("spot")    if r.get("spot")    is not None else "",
            r.get("premium") if r.get("premium") is not None else "",
            meta_str,
        ])

    buf.seek(0)

    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS os_stage (LIKE option_selection INCLUDING ALL)
        ON COMMIT DROP
    """)
    conn.execute("TRUNCATE os_stage")

    with conn.cursor() as cur:
        with cur.copy(
            "COPY os_stage "
            "(underlying_id, expiry, ts, rule_id, label, option_type, strike, "
            " instrument_id, spot, premium, meta) "
            "FROM STDIN (FORMAT CSV, NULL '')"
        ) as copy:
            copy.write(buf.getvalue())

    result = conn.execute("""
        INSERT INTO option_selection
            (underlying_id, expiry, ts, rule_id, label, option_type, strike,
             instrument_id, spot, premium, meta)
        SELECT underlying_id, expiry, ts, rule_id, label, option_type, strike,
               instrument_id, spot, premium, meta
        FROM os_stage
        ON CONFLICT (underlying_id, expiry, ts, rule_id, label, option_type)
        DO UPDATE SET
            strike        = EXCLUDED.strike,
            instrument_id = EXCLUDED.instrument_id,
            spot          = EXCLUDED.spot,
            premium       = EXCLUDED.premium,
            meta          = EXCLUDED.meta
    """)
    return result.rowcount if result.rowcount >= 0 else 0


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def fetch_instruments_for_underlying(
    conn: psycopg.Connection,
    underlying_id: int,
) -> list[dict[str, Any]]:
    """Return all instruments belonging to an underlying."""
    rows = conn.execute(
        """
        SELECT instrument_id, raw_ticker, instrument_type, exchange,
               expiry, strike, option_type, lot_size, is_active
        FROM instrument
        WHERE underlying_id = %s
        ORDER BY instrument_type, expiry, strike, option_type
        """,
        (underlying_id,),
    ).fetchall()
    cols = ["instrument_id", "raw_ticker", "instrument_type", "exchange",
            "expiry", "strike", "option_type", "lot_size", "is_active"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_ohlcv_for_instrument(
    conn: psycopg.Connection,
    instrument_id: int,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return OHLCV rows for an instrument, optionally filtered by time range."""
    params: list[Any] = [instrument_id]
    where = "WHERE instrument_id = %s"
    if start_ts:
        where += " AND ts >= %s"
        params.append(start_ts)
    if end_ts:
        where += " AND ts <= %s"
        params.append(end_ts)

    rows = conn.execute(
        f"SELECT instrument_id, ts, open, high, low, close, volume, open_interest "
        f"FROM ohlcv {where} ORDER BY ts",
        params,
    ).fetchall()
    cols = ["instrument_id", "ts", "open", "high", "low", "close", "volume", "open_interest"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_all_underlyings(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Return all underlyings."""
    rows = conn.execute(
        "SELECT underlying_id, symbol, kind, exchange, strike_step, lot_size FROM underlying ORDER BY symbol"
    ).fetchall()
    cols = ["underlying_id", "symbol", "kind", "exchange", "strike_step", "lot_size"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_active_selection_rules(
    conn: psycopg.Connection,
    underlying_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return active selection rules, optionally filtered by underlying."""
    params: list[Any] = []
    where = "WHERE is_active = TRUE"
    if underlying_id is not None:
        where += " AND underlying_id = %s"
        params.append(underlying_id)

    rows = conn.execute(
        f"SELECT rule_id, underlying_id, method, params FROM selection_rule {where}",
        params,
    ).fetchall()
    cols = ["rule_id", "underlying_id", "method", "params"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: Any) -> str:
    """Format a datetime (or string) for CSV COPY."""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S%z")
    return str(ts)
