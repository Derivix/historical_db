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

    def find_nearest_complete_expiry(
        self, underlying_id: int, start_date: str, end_date: str,
    ) -> dt.date | None:
        """Return the first expiry for which CE and PE instruments exist.

        The range strategy uses only the nearest expiry.  ``LIMIT 1`` avoids
        materialising every expiry in the 45-day search window on large
        instrument tables.
        """
        q = text("""
            SELECT ce.expiry
            FROM instrument AS ce
            WHERE ce.underlying_id = :uid
              AND ce.instrument_type = 'CE'
              AND ce.is_active = TRUE
              AND ce.expiry BETWEEN CAST(:start_date AS DATE) AND CAST(:end_date AS DATE)
              AND EXISTS (
                  SELECT 1
                  FROM instrument AS pe
                  WHERE pe.underlying_id = ce.underlying_id
                    AND pe.instrument_type = 'PE'
                    AND pe.is_active = TRUE
                    AND pe.expiry = ce.expiry
              )
            ORDER BY ce.expiry
            LIMIT 1
        """)
        with self.engine.connect() as conn:
            row = conn.execute(q, {
                "uid": underlying_id, "start_date": start_date, "end_date": end_date,
            }).first()
        return row[0] if row else None

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

    def get_option_candidates_at(
        self,
        underlying_id: int,
        expiry: dt.date,
        option_type: str,
        min_strike: float,
        max_strike: float,
        day: dt.date,
        when: dt.datetime,
    ) -> list[dict]:
        """Return option candidates with their last price at ``when``.

        This avoids opening one database connection for every strike while a
        strategy searches for a premium.  The lateral lookup is backward-only,
        so it preserves the no-look-ahead behaviour of the backtester.
        """
        q = text("""
            SELECT i.instrument_id, i.raw_ticker, i.lot_size, i.strike, px.close AS price
            FROM instrument AS i
            JOIN LATERAL (
                SELECT o.close
                FROM ohlcv AS o
                WHERE o.instrument_id = i.instrument_id
                  AND o.ts >= CAST(:day AS DATE)
                  AND o.ts <= :when
                ORDER BY o.ts DESC
                LIMIT 1
            ) AS px ON TRUE
            WHERE i.underlying_id = :uid
              AND i.instrument_type = :otype
              AND i.expiry = :expiry
              AND i.is_active = TRUE
              AND i.strike BETWEEN :min_strike AND :max_strike
            ORDER BY i.strike
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(q, {
                "uid": underlying_id, "otype": option_type, "expiry": expiry,
                "min_strike": min_strike, "max_strike": max_strike,
                "day": day, "when": when,
            }).mappings().all()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # OHLCV series
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_timestamp_column(df: pd.DataFrame) -> pd.DataFrame:
        """Return naïve Asia/Kolkata timestamps for either DB timestamp form.

        Some database imports store ``timestamp without time zone`` while
        others preserve ``timestamp with time zone``.  The former must be
        localized before conversion; calling ``tz_convert`` directly on it
        raises the pandas error seen by the backtest runner.
        """
        timestamps = pd.to_datetime(df["ts"])
        if timestamps.dt.tz is None:
            timestamps = timestamps.dt.tz_localize("Asia/Kolkata")
        else:
            timestamps = timestamps.dt.tz_convert("Asia/Kolkata")
        df["ts"] = timestamps.dt.tz_localize(None)
        return df

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
        return self._normalise_timestamp_column(df)

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
        return self._normalise_timestamp_column(df)

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
