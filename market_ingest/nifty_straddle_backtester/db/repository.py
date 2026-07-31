"""
Data-access layer.

Maps directly onto the schema created by your `market-ingest migrate` DDL:

    underlying(underlying_id, symbol, kind, exchange, strike_step, lot_size, ...)
    instrument(instrument_id, raw_ticker, underlying_id, instrument_type,
               exchange, expiry, strike, option_type, lot_size, is_active, ...)
    ohlcv(instrument_id, ts, open, high, low, close, volume, open_interest)

No business logic lives here - this module only knows how to fetch rows and
hand back tidy pandas DataFrames. The strategy/engine layers never write SQL
directly, so if you swap Postgres for something else later, only this file
changes.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

try:
    from sqlalchemy import text
except ImportError:  # pragma: no cover - only needed when hitting a real Postgres DSN
    text = None


class MarketDataRepository:
    def __init__(self, dsn: str):
        """
        dsn: SQLAlchemy-style Postgres DSN, e.g.
             postgresql+psycopg2://user:pass@host:5432/market

        sqlalchemy is imported lazily here (rather than at module level) so
        the rest of the framework - and the synthetic demo repo - can be
        imported/run in environments where the DB driver isn't installed.
        """
        from sqlalchemy import create_engine
        self.engine = create_engine(dsn, pool_pre_ping=True)

    # ------------------------------------------------------------------
    # Underlying / instrument metadata
    # ------------------------------------------------------------------

    def get_underlying(self, symbol: str, kind: str = "INDEX") -> dict:
        q = text("""
            SELECT underlying_id, symbol, kind, exchange, strike_step, lot_size
            FROM underlying
            WHERE symbol = :symbol AND kind = :kind
        """)
        with self.engine.connect() as conn:
            row = conn.execute(q, {"symbol": symbol, "kind": kind}).mappings().first()
        if row is None:
            raise ValueError(f"No underlying found for symbol={symbol!r} kind={kind!r}")
        return dict(row)

    def get_index_instrument_id(self, underlying_id: int) -> int:
        """The INDEX instrument row that carries the spot OHLCV series."""
        q = text("""
            SELECT instrument_id FROM instrument
            WHERE underlying_id = :uid AND instrument_type = 'FUT' AND continuous_rank = 0
            LIMIT 1
        """)
        with self.engine.connect() as conn:
            row = conn.execute(q, {"uid": underlying_id}).first()
        if row is None:
            raise ValueError(f"No INDEX instrument row for underlying_id={underlying_id}")
        return row[0]

    # list of expiries for a given underlying in a date range, for which CE/PE instruments exist
    def list_expiries(self, underlying_id: int, start_date: str, end_date: str) -> list[dt.date]:
        q = text("""
            SELECT DISTINCT expiry FROM instrument
            WHERE underlying_id = :uid
              AND instrument_type IN ('CE','PE')
              AND expiry BETWEEN :start_date AND :end_date
            ORDER BY expiry
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(q, {"uid": underlying_id, "start_date": start_date, "end_date": end_date}).fetchall()
        return [r[0] for r in rows]

    def get_option_instrument(
        self, underlying_id: int, expiry: dt.date, strike: float, option_type: str
    ) -> Optional[dict]:
        """Resolve the (expiry, strike, CE/PE) tuple to a concrete instrument row."""
        q = text("""
            SELECT instrument_id, raw_ticker, lot_size
            FROM instrument
            WHERE underlying_id = :uid
              AND instrument_type = :otype
              AND expiry = :expiry
              AND strike = :strike
              AND is_active = TRUE
            LIMIT 1
        """)
        with self.engine.connect() as conn:
            row = conn.execute(q, {
                "uid": underlying_id, "otype": option_type, "expiry": expiry, "strike": strike
            }).mappings().first()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # OHLCV series
    # ------------------------------------------------------------------

    def get_spot_ohlcv(self, instrument_id: int, start_date: str, end_date: str) -> pd.DataFrame:
        # q = text("""
        #     SELECT ts, open, high, low, close, volume
        #     FROM ohlcv
        #     WHERE instrument_id = :iid
        #       AND ts >= :start_date AND ts < (:end_date::date + INTERVAL '1 day')
        #     ORDER BY ts
        # """)
        q = text("""
            SELECT ts, open, high, low, close, volume
            FROM ohlcv
            WHERE instrument_id = :iid
            AND ts >= :start_date
            AND ts < (CAST(:end_date AS DATE) + INTERVAL '1 day')
            ORDER BY ts
        """)
        with self.engine.connect() as conn:
            df = pd.read_sql(q, conn, params={"iid": instrument_id, "start_date": start_date, "end_date": end_date})
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return df

    def get_option_ohlcv(self, instrument_id: int, day: dt.date) -> pd.DataFrame:
        # q = text("""
        #     SELECT ts, open, high, low, close, volume, open_interest
        #     FROM ohlcv
        #     WHERE instrument_id = :iid
        #       AND ts >= :day::date AND ts < (:day::date + INTERVAL '1 day')
        #     ORDER BY ts
        # """)
        q = text("""
            SELECT ts, open, high, low, close, volume, open_interest
            FROM ohlcv
            WHERE instrument_id = :iid
            AND ts >= CAST(:day AS DATE)
            AND ts < (CAST(:day AS DATE) + INTERVAL '1 day')
            ORDER BY ts
        """)
        with self.engine.connect() as conn:
            df = pd.read_sql(q, conn, params={"iid": instrument_id, "day": day})
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return df

    def get_trading_days(self, instrument_id: int, start_date: str, end_date: str) -> list[dt.date]:
        # q = text("""
        #     SELECT DISTINCT ts::date AS d FROM ohlcv
        #     WHERE instrument_id = :iid
        #       AND ts >= :start_date AND ts < (:end_date::date + INTERVAL '1 day')
        #     ORDER BY d
        # """)
        q = text("""
            SELECT DISTINCT ts::date AS d
            FROM ohlcv
            WHERE instrument_id = :iid
            AND ts >= :start_date
            AND ts < (CAST(:end_date AS DATE) + INTERVAL '1 day')
            ORDER BY d
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(q, {"iid": instrument_id, "start_date": start_date, "end_date": end_date}).fetchall()
        return [r[0] for r in rows]
