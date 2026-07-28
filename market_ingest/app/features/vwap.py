"""
Session VWAP per instrument per day.

Formula: cumulative sum(close * volume) / sum(volume) within each trading day.

VWAP is computed in polars (fast, vectorised) then bulk-loaded into the
`feature` table via COPY.
"""
from __future__ import annotations

from datetime import date, timezone
from typing import Any

import polars as pl
import structlog

from app.db.connection import get_connection
from app.db import dao

logger = structlog.get_logger(__name__)


def compute_and_store_vwap(
    instrument_ids: list[int] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    """
    Compute session VWAP for all (or specified) instruments and store in
    the `feature` table.

    Parameters
    ----------
    instrument_ids:
        If None, process all instruments.
    start_date / end_date:
        Optional date range filter for ohlcv rows.

    Returns
    -------
    Total number of feature rows upserted.
    """
    with get_connection() as conn:
        # Fetch relevant OHLCV rows from DB
        rows = _fetch_ohlcv(conn, instrument_ids, start_date, end_date)
        if not rows:
            logger.info("vwap_no_data")
            return 0

        df = pl.DataFrame(rows)
        feature_rows = _compute_vwap(df)

        if feature_rows.is_empty():
            logger.info("vwap_empty_result")
            return 0

        # Convert to list of dicts for bulk COPY
        records = feature_rows.to_dicts()
        total = dao.copy_feature_rows(conn, records)
        conn.commit()

    logger.info("vwap_stored", rows=total)
    return total


def _fetch_ohlcv(
    conn: Any,
    instrument_ids: list[int] | None,
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    """Fetch ohlcv rows needed for VWAP computation."""
    params: list[Any] = []
    where_parts = []

    if instrument_ids:
        placeholders = ",".join(["%s"] * len(instrument_ids))
        where_parts.append(f"instrument_id IN ({placeholders})")
        params.extend(instrument_ids)

    if start_date:
        where_parts.append("ts >= %s")
        params.append(start_date)

    if end_date:
        where_parts.append("ts < %s")
        params.append(end_date)

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = conn.execute(
        f"SELECT instrument_id, ts, close, volume FROM ohlcv {where} ORDER BY instrument_id, ts",
        params,
    ).fetchall()

    return [
        {"instrument_id": r[0], "ts": r[1], "close": float(r[2]), "volume": int(r[3])}
        for r in rows
    ]


def _compute_vwap(df: pl.DataFrame) -> pl.DataFrame:
    """
    Compute cumulative session VWAP per instrument per day using polars.

    VWAP_i = cumsum(close_i * volume_i) / cumsum(volume_i)
    where i iterates within the same (instrument_id, date) partition.
    """
    # Add trading date column (UTC date — consistent since we store UTC)
    df = df.with_columns(
        pl.col("ts").dt.date().alias("trade_date")
    )

    # Sort for cumulative correctness
    df = df.sort(["instrument_id", "trade_date", "ts"])

    # Compute cv (close * volume)
    df = df.with_columns(
        (pl.col("close") * pl.col("volume")).alias("cv")
    )

    # Cumulative sums within (instrument_id, trade_date)
    df = df.with_columns([
        pl.col("cv").cum_sum().over(["instrument_id", "trade_date"]).alias("cum_cv"),
        pl.col("volume").cum_sum().over(["instrument_id", "trade_date"]).alias("cum_vol"),
    ])

    # VWAP = cum_cv / cum_vol (null if cum_vol == 0)
    df = df.with_columns(
        (
            pl.when(pl.col("cum_vol") > 0)
            .then(pl.col("cum_cv") / pl.col("cum_vol"))
            .otherwise(None)
        ).alias("vwap")
    )

    return df.select(["instrument_id", "ts", "vwap"])


def compute_vwap_in_memory(
    ohlcv_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Compute session VWAP from an in-memory list of OHLCV row dicts.
    Useful for testing without a database.

    Each dict: instrument_id, ts (datetime), close (float), volume (int).
    Returns list of dicts: instrument_id, ts, vwap.
    """
    if not ohlcv_rows:
        return []

    df = pl.DataFrame(ohlcv_rows)
    result = _compute_vwap(df)
    return result.to_dicts()
