"""
Ingestion pipeline: read → map → parse → validate → load

Orchestrates all ingest sub-modules in a single pass over the source file.
Supports both CSV and XLSX.  Processes data in configurable batches.
"""
from __future__ import annotations

import zoneinfo
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import structlog

from app.config import ColumnMapProfile, Settings, get_settings, load_profile
from app.db.connection import get_connection
from app.db import dao
from app.ingest.column_mapper import map_columns, ColumnMapError
from app.ingest.reader import read_file_batches
from app.ingest.ticker_parser import (
    parse_ticker, infer_underlying_kind, TickerParseError
)
from app.ingest.validator import validate_batch, RejectReport

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------

class IngestResult:
    def __init__(self) -> None:
        self.files_processed: int = 0
        self.rows_read: int = 0
        self.rows_inserted: int = 0
        self.rows_rejected: int = 0
        self.reject_report = RejectReport()
        self.errors: list[str] = []

    def merge(self, other: "IngestResult") -> None:
        self.files_processed += other.files_processed
        self.rows_read += other.rows_read
        self.rows_inserted += other.rows_inserted
        self.rows_rejected += other.rows_rejected
        self.reject_report.rejects.extend(other.reject_report.rejects)
        self.errors.extend(other.errors)

    def summary(self) -> str:
        return (
            f"Files={self.files_processed} "
            f"Read={self.rows_read} "
            f"Inserted={self.rows_inserted} "
            f"Rejected={self.rows_rejected} "
            f"Errors={len(self.errors)}"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ingest_path(
    path: str | Path,
    profile_name: str | None = None,
    strict: bool = False,
    settings: Settings | None = None,
) -> IngestResult:
    """
    Ingest one file or all CSV/XLSX files in a directory.

    Parameters
    ----------
    path:
        File or directory path.
    profile_name:
        Name of a profile in config/profiles/ (without .yaml).
        If None, uses settings.ingest.default_profile.
    strict:
        If True, abort on first row validation error.
    settings:
        Application settings (loaded from config if None).
    """
    if settings is None:
        settings = get_settings()

    p = Path(path)
    if p.is_dir():
        files = list(p.glob("*.csv")) + list(p.glob("*.xlsx")) + list(p.glob("*.xls"))
    elif p.is_file():
        files = [p]
    else:
        raise FileNotFoundError(f"Path does not exist: {path}")

    if not files:
        logger.warning("no_files_found", path=str(path))
        return IngestResult()

    combined = IngestResult()
    for f in files:
        result = ingest_file(f, profile_name=profile_name, strict=strict, settings=settings)
        combined.merge(result)

    logger.info("ingest_path_done", path=str(path), summary=combined.summary())
    return combined


def ingest_file(
    file_path: str | Path,
    profile_name: str | None = None,
    strict: bool = False,
    settings: Settings | None = None,
) -> IngestResult:
    """Ingest a single file."""
    if settings is None:
        settings = get_settings()

    pname = profile_name or settings.ingest.default_profile
    profile = load_profile(pname, settings.ingest.profiles_dir)
    result = IngestResult()
    result.files_processed = 1

    logger.info("ingest_file_start", file=str(file_path), profile=pname)

    # Cache for underlying_id / instrument_id lookups (per-file)
    underlying_cache: dict[tuple[str, str], int] = {}
    instrument_cache: dict[str, int] = {}

    tz_source = zoneinfo.ZoneInfo(profile.timezone)

    try:
        batches = list(read_file_batches(file_path, batch_size=settings.ingest.batch_size))
    except Exception as exc:
        result.errors.append(f"Cannot read file {file_path}: {exc}")
        logger.error("file_read_failed", file=str(file_path), error=str(exc))
        return result

    if not batches:
        logger.warning("empty_file", file=str(file_path))
        return result

    # Detect column mapping from first batch headers
    first_headers = batches[0].columns
    try:
        mapping = map_columns(first_headers, profile)
    except ColumnMapError as exc:
        result.errors.append(str(exc))
        logger.error("column_map_failed", file=str(file_path), error=str(exc))
        return result

    # Source column names
    col_ticker = mapping.require("ticker")
    col_date   = mapping.require("date")
    col_open   = mapping.require("open")
    col_high   = mapping.require("high")
    col_low    = mapping.require("low")
    col_close  = mapping.require("close")
    col_volume = mapping.get("volume")
    col_time   = mapping.get("time")
    col_oi     = mapping.get("open_interest")

    row_counter = 0

    with get_connection() as conn:
        for batch_df in batches:
            ohlcv_rows: list[dict[str, Any]] = []

            for row_idx in range(len(batch_df)):
                row_counter += 1
                result.rows_read += 1
                row = batch_df.row(row_idx, named=True)

                raw_ticker = _str(row.get(col_ticker))
                if not raw_ticker:
                    continue

                # Parse timestamp
                try:
                    ts_utc = _parse_timestamp(
                        row.get(col_date), row.get(col_time) if col_time else None,
                        profile, tz_source,
                    )
                except Exception as exc:
                    result.reject_report.add_reject_simple(
                        row_counter, raw_ticker, str(row.get(col_date)), f"Bad timestamp: {exc}"
                    )
                    result.rows_rejected += 1
                    if strict:
                        raise ValueError(f"Strict mode: {exc}") from exc
                    continue

                # Parse numeric fields
                open_  = _to_float(row.get(col_open))
                high   = _to_float(row.get(col_high))
                low    = _to_float(row.get(col_low))
                close  = _to_float(row.get(col_close))

                volume_raw = row.get(col_volume) if col_volume else None
                volume = _to_int(volume_raw) if (volume_raw is not None and _str(volume_raw) != "") else 0

                oi_raw = row.get(col_oi) if col_oi else None
                oi = _to_int_opt(oi_raw) if (oi_raw is not None and _str(oi_raw) != "") else 0
                if oi is None:
                    oi = 0

                # Validate row
                from app.ingest.validator import validate_row, RowReject
                ok = validate_row(
                    row_index=row_counter,
                    raw_ticker=raw_ticker,
                    ts=ts_utc,
                    open_=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=float(volume),
                    open_interest=float(oi) if oi is not None else None,
                    strict=strict,
                    reject_report=result.reject_report,
                )
                if not ok:
                    result.rows_rejected += 1
                    continue

                # Parse ticker → instrument
                try:
                    parsed = parse_ticker(raw_ticker)
                except TickerParseError as exc:
                    result.reject_report.add_reject_simple(
                        row_counter, raw_ticker, str(ts_utc), f"TickerParseError: {exc}"
                    )
                    result.rows_rejected += 1
                    continue

                # Upsert underlying
                ukey = (parsed.symbol, infer_underlying_kind(parsed.symbol, parsed.instrument_type))
                if ukey not in underlying_cache:
                    underlying_cache[ukey] = dao.upsert_underlying(
                        conn,
                        symbol=parsed.symbol,
                        kind=ukey[1],
                        exchange=parsed.segment,
                    )
                    conn.commit()
                underlying_id = underlying_cache[ukey]

                # Upsert instrument
                if raw_ticker not in instrument_cache:
                    instrument_cache[raw_ticker] = dao.upsert_instrument(
                        conn,
                        raw_ticker=raw_ticker,
                        underlying_id=underlying_id,
                        instrument_type=parsed.instrument_type,
                        exchange=parsed.segment,
                        expiry=parsed.expiry,
                        strike=Decimal(str(parsed.strike)) if parsed.strike is not None else None,
                        option_type=parsed.option_type,
                        continuous_rank=parsed.continuous_rank,
                    )
                    conn.commit()
                instrument_id = instrument_cache[raw_ticker]

                ohlcv_rows.append({
                    "instrument_id": instrument_id,
                    "ts": ts_utc,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "open_interest": oi,
                })

            if ohlcv_rows:
                inserted = dao.copy_ohlcv_rows(conn, ohlcv_rows)
                conn.commit()
                result.rows_inserted += inserted
                logger.info(
                    "batch_loaded",
                    file=str(file_path),
                    batch_rows=len(ohlcv_rows),
                    inserted=inserted,
                )

    logger.info(
        "ingest_file_done",
        file=str(file_path),
        summary=result.summary(),
    )
    if not result.reject_report.is_empty():
        logger.warning("rejects_summary", text=result.reject_report.summary())

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _to_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _to_int(val: Any) -> int:
    if val is None or val == "":
        return 0
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return 0


def _to_int_opt(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _parse_timestamp(
    date_val: Any,
    time_val: Any,
    profile: ColumnMapProfile,
    tz_source: Any,
) -> datetime:
    """
    Combine date and time strings into a UTC-aware datetime.

    The profile's datetime_format is used; if a separate time column exists
    the format is applied to 'DATE TIME' combined string.
    If only date is present (daily), time is set to 00:00:00.
    """
    date_str = _str(date_val)
    if not date_str:
        raise ValueError("Empty date value")

    if time_val is not None and _str(time_val):
        time_str = _str(time_val)
        combined = f"{date_str} {time_str}"
        fmt = profile.datetime_format

        # If the profile format doesn't include time, append a standard time format.
        # Accept timezone offsets like +05:30 when present in the incoming time value.
        if "%H" not in fmt and "%I" not in fmt:
            fmt = fmt + " %H:%M:%S"
            if time_str.endswith(("+00:00", "+01:00", "+02:00", "+03:00", "+04:00", "+05:00", "+05:30", "+06:00", "+07:00", "+08:00", "+09:00", "+10:00", "+11:00", "+12:00", "-01:00", "-02:00", "-03:00", "-04:00", "-05:00", "-06:00", "-07:00", "-08:00", "-09:00", "-10:00", "-11:00", "-12:00")):
                fmt = fmt + "%z"
        try:
            dt_naive = datetime.strptime(combined, fmt)
        except ValueError:
            if "%z" not in fmt:
                dt_naive = datetime.strptime(combined, fmt + "%z")
            else:
                raise
    else:
        dt_naive = datetime.strptime(date_str, profile.datetime_format)

    # Attach source timezone then convert to UTC
    if dt_naive.tzinfo is None:
        dt_local = dt_naive.replace(tzinfo=tz_source)
    else:
        dt_local = dt_naive.astimezone(tz_source)

    # Normalize to minute precision for consistent OHLCV bars across providers.
    dt_local = dt_local.replace(second=0, microsecond=0)
    dt_utc = dt_local.astimezone(timezone.utc)
    return dt_utc


# Patch RejectReport to support add_reject_simple
from app.ingest.validator import RejectReport, RowReject  # noqa: E402

def _add_reject_simple(self: RejectReport, row_index: int, raw_ticker: str, ts: str, reason: str) -> None:
    self.add(RowReject(row_index=row_index, raw_ticker=raw_ticker, ts=ts, reason=reason, raw_values={}))

RejectReport.add_reject_simple = _add_reject_simple  # type: ignore[attr-defined]
