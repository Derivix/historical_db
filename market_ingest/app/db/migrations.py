"""
Idempotent database schema migrations.

Run via: market-ingest migrate
Or directly: python -m app.db.migrations

All DDL uses IF NOT EXISTS / DO NOTHING guards so re-running is safe.
TimescaleDB extension, hypertables, compression policies, and continuous
aggregates are all created idempotently.
"""
from __future__ import annotations

import structlog

from app.db.connection import get_raw_connection

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# DDL statements
# ---------------------------------------------------------------------------

_DDL_EXTENSION = "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"

_DDL_UNDERLYING = """
CREATE TABLE IF NOT EXISTS underlying (
    underlying_id   SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('INDEX', 'STOCK')),
    exchange        TEXT NOT NULL,
    strike_step     NUMERIC(12, 2),
    lot_size        INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, kind)
);
"""

_DDL_INSTRUMENT = """
CREATE TABLE IF NOT EXISTS instrument (
    instrument_id   BIGSERIAL PRIMARY KEY,
    raw_ticker      TEXT UNIQUE,
    underlying_id   INT REFERENCES underlying (underlying_id),
    instrument_type TEXT CHECK (instrument_type IN ('INDEX', 'EQ', 'FUT', 'CE', 'PE')),
    exchange        TEXT NOT NULL,
    expiry          DATE,
    strike          NUMERIC(12, 2),
    option_type     TEXT,
    lot_size        INT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- options must have expiry, strike, option_type; non-options must not have strike/option_type
    CONSTRAINT options_require_fields CHECK (
        (instrument_type IN ('CE', 'PE') AND expiry IS NOT NULL AND strike IS NOT NULL AND option_type IS NOT NULL)
        OR (instrument_type NOT IN ('CE', 'PE') AND strike IS NULL AND option_type IS NULL)
        OR instrument_type IS NULL
    ),
    -- option_type must equal instrument_type when set
    CONSTRAINT option_type_matches CHECK (
        option_type IS NULL OR option_type = instrument_type
    )
);
"""

_DDL_INSTRUMENT_CONTINUOUS_RANK = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'instrument' AND column_name = 'continuous_rank'
    ) THEN
        ALTER TABLE instrument ADD COLUMN continuous_rank SMALLINT;
    END IF;
END $$;
"""

_DDL_INSTRUMENT_UNIQUE_IDX = """
DO $$
BEGIN
    -- Drop old index (without continuous_rank) if it exists, then recreate.
    -- This is safe: re-running drops/creates a valid index each time.
    DROP INDEX IF EXISTS instrument_composite_uq;

    CREATE UNIQUE INDEX instrument_composite_uq
    ON instrument (
        underlying_id,
        instrument_type,
        COALESCE(expiry,            '1900-01-01'::DATE),
        COALESCE(strike,            -1),
        COALESCE(option_type,       'X'),
        COALESCE(continuous_rank,   0)
    );
END $$;
"""

_DDL_INSTRUMENT_INDEXES = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE tablename = 'instrument' AND indexname = 'instrument_expiry_strike_idx'
    ) THEN
        CREATE INDEX instrument_expiry_strike_idx
        ON instrument (underlying_id, expiry, strike, option_type);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE tablename = 'instrument' AND indexname = 'instrument_type_idx'
    ) THEN
        CREATE INDEX instrument_type_idx
        ON instrument (underlying_id, instrument_type);
    END IF;
END $$;
"""

_DDL_OHLCV = """
CREATE TABLE IF NOT EXISTS ohlcv (
    instrument_id   BIGINT NOT NULL REFERENCES instrument (instrument_id),
    ts              TIMESTAMPTZ NOT NULL,
    open            NUMERIC(14, 4) NOT NULL,
    high            NUMERIC(14, 4) NOT NULL,
    low             NUMERIC(14, 4) NOT NULL,
    close           NUMERIC(14, 4) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    open_interest   BIGINT,
    PRIMARY KEY (instrument_id, ts),
    CHECK (high >= low),
    CHECK (open >= 0),
    CHECK (high >= 0),
    CHECK (low >= 0),
    CHECK (close >= 0),
    CHECK (volume >= 0)
);
"""

_DDL_OHLCV_HYPERTABLE = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'ohlcv'
    ) THEN
        PERFORM create_hypertable('ohlcv', 'ts', chunk_time_interval => INTERVAL '1 day');
    END IF;
END $$;
"""

_DDL_OHLCV_COMPRESSION = """
DO $$
BEGIN
    -- Enable compression if not already enabled
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertable_compression_settings
        WHERE hypertable::text = 'ohlcv'
          AND compress_interval_length IS NOT NULL
    ) THEN
        ALTER TABLE ohlcv SET (
            timescaledb.compress = true,
            timescaledb.compress_segmentby = 'instrument_id',
            timescaledb.compress_orderby = 'ts DESC'
        );
    END IF;

    -- Add compression policy (compress chunks older than 7 days)
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression'
          AND hypertable_name = 'ohlcv'
    ) THEN
        PERFORM add_compression_policy('ohlcv', INTERVAL '7 days');
    END IF;
END $$;
"""

_DDL_SELECTION_RULE = """
CREATE TABLE IF NOT EXISTS selection_rule (
    rule_id         SERIAL PRIMARY KEY,
    underlying_id   INT REFERENCES underlying (underlying_id),
    method          TEXT NOT NULL CHECK (method IN ('OFFSET', 'PREMIUM_NEAR', 'PREMIUM_LTE', 'PREMIUM_GTE', 'DELTA')),
    params          JSONB NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DDL_OPTION_SELECTION = """
CREATE TABLE IF NOT EXISTS option_selection (
    underlying_id   INT NOT NULL REFERENCES underlying (underlying_id),
    expiry          DATE NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    rule_id         INT NOT NULL REFERENCES selection_rule (rule_id),
    label           TEXT NOT NULL,
    option_type     TEXT NOT NULL CHECK (option_type IN ('CE', 'PE')),
    strike          NUMERIC(12, 2) NOT NULL,
    instrument_id   BIGINT REFERENCES instrument (instrument_id),
    spot            NUMERIC(14, 4),
    premium         NUMERIC(14, 4),
    meta            JSONB,
    PRIMARY KEY (underlying_id, expiry, ts, rule_id, label, option_type)
);
"""

_DDL_OPTION_SELECTION_HYPERTABLE = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'option_selection'
    ) THEN
        PERFORM create_hypertable('option_selection', 'ts', chunk_time_interval => INTERVAL '1 day');
    END IF;
END $$;
"""

_DDL_OPTION_SELECTION_INDEXES = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE tablename = 'option_selection' AND indexname = 'os_rule_label_idx'
    ) THEN
        CREATE INDEX os_rule_label_idx ON option_selection (underlying_id, expiry, rule_id, label, ts);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE tablename = 'option_selection' AND indexname = 'os_instrument_ts_idx'
    ) THEN
        CREATE INDEX os_instrument_ts_idx ON option_selection (instrument_id, ts);
    END IF;
END $$;
"""

_DDL_FEATURE = """
CREATE TABLE IF NOT EXISTS feature (
    instrument_id   BIGINT NOT NULL REFERENCES instrument (instrument_id),
    ts              TIMESTAMPTZ NOT NULL,
    vwap            NUMERIC(14, 4),
    iv              NUMERIC(10, 6),
    delta           NUMERIC(10, 6),
    gamma           NUMERIC(12, 8),
    theta           NUMERIC(12, 6),
    vega            NUMERIC(12, 6),
    PRIMARY KEY (instrument_id, ts)
);
"""

_DDL_FEATURE_HYPERTABLE = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'feature'
    ) THEN
        PERFORM create_hypertable('feature', 'ts', chunk_time_interval => INTERVAL '1 day');
    END IF;
END $$;
"""

_DDL_OHLCV_5M_CAGG = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.continuous_aggregates
        WHERE view_name = 'ohlcv_5m'
    ) THEN
        CREATE MATERIALIZED VIEW ohlcv_5m
        WITH (timescaledb.continuous) AS
        SELECT
            instrument_id,
            time_bucket('5 minutes', ts)                       AS ts,
            first(open, ts)                                    AS open,
            max(high)                                          AS high,
            min(low)                                           AS low,
            last(close, ts)                                    AS close,
            sum(volume)                                        AS volume,
            last(open_interest, ts)                            AS open_interest,
            sum(close * volume) / NULLIF(sum(volume), 0)       AS vwap
        FROM ohlcv
        GROUP BY instrument_id, time_bucket('5 minutes', ts)
        WITH NO DATA;
    END IF;
END $$;
"""

_DDL_OHLCV_5M_REFRESH_POLICY = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_refresh_continuous_aggregate'
          AND hypertable_name = 'ohlcv_5m'
    ) THEN
        PERFORM add_continuous_aggregate_policy(
            'ohlcv_5m',
            start_offset => INTERVAL '30 days',
            end_offset   => INTERVAL '1 hour',
            schedule_interval => INTERVAL '1 hour'
        );
    END IF;
END $$;
"""

# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def run_migrations(dsn: str | None = None) -> None:
    """Apply all DDL statements idempotently."""
    steps = [
        ("timescaledb_extension",           _DDL_EXTENSION),
        ("underlying_table",                _DDL_UNDERLYING),
        ("instrument_table",                _DDL_INSTRUMENT),
        ("instrument_continuous_rank_col",  _DDL_INSTRUMENT_CONTINUOUS_RANK),
        ("instrument_composite_unique_idx", _DDL_INSTRUMENT_UNIQUE_IDX),
        ("instrument_search_indexes",       _DDL_INSTRUMENT_INDEXES),
        ("ohlcv_table",                     _DDL_OHLCV),
        ("ohlcv_hypertable",                _DDL_OHLCV_HYPERTABLE),
        ("ohlcv_compression",               _DDL_OHLCV_COMPRESSION),
        ("selection_rule_table",            _DDL_SELECTION_RULE),
        ("option_selection_table",          _DDL_OPTION_SELECTION),
        ("option_selection_hypertable",     _DDL_OPTION_SELECTION_HYPERTABLE),
        ("option_selection_indexes",        _DDL_OPTION_SELECTION_INDEXES),
        ("feature_table",                   _DDL_FEATURE),
        ("feature_hypertable",              _DDL_FEATURE_HYPERTABLE),
        ("ohlcv_5m_cagg",                   _DDL_OHLCV_5M_CAGG),
        ("ohlcv_5m_refresh_policy",         _DDL_OHLCV_5M_REFRESH_POLICY),
    ]

    with get_raw_connection(dsn) as conn:
        for name, ddl in steps:
            logger.info("migration_step", step=name)
            try:
                conn.execute(ddl)
                logger.info("migration_step_ok", step=name)
            except Exception as exc:
                logger.error("migration_step_failed", step=name, error=str(exc))
                raise

    logger.info("migrations_complete", steps=len(steps))


if __name__ == "__main__":
    import structlog
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(20))
    run_migrations()
