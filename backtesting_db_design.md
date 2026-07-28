# Backtesting Data Platform — Design (Concise)

**Scope:** Store index / stock / options OHLCV for a backtesting engine with configurable option selection.

---

## 1. What & Why

- **Job:** store historical prices so a backtest can query them fast.
- **Data:** price rows with OHLC, Volume, sometimes Open Interest. Instrument encoded as a code, e.g. `AARTIIND27JUN24700CE.NFO` → underlying `AARTIIND`, expiry `27JUN24`, strike `700`, type `CE` (call) / `PE` (put).
- **DB choice:** price data → **PostgreSQL + TimescaleDB**. Strategies + results → **MongoDB** (existing). Rule: *prices in Postgres, app data in Mongo.*
- **Why Timescale over Mongo for prices:** time-series native (auto time-partitioning), ~10× compression on old data, real joins for parent↔option links, window functions for VWAP/ATM.
- **Not kdb+ / ClickHouse:** kdb+ = costly + niche language; ClickHouse = weak on updates/relations the selection layer needs.

---

## 2. Core Rules (don't break)

1. Bars key on integer **`instrument_id`**, never the ticker string (ticker stored once for audit).
2. **`underlying`** is the hub — an index/stock and all its options share one `underlying_id`.
3. **Moneyness/selection is time-varying** — never a column on `instrument`/`ohlcv`; computed per-timestamp into snapshot tables.
4. **Selection method is config**, not schema — new method = new rule row + small ETL function, no migration.
5. **Derived fields** (VWAP, greeks) live in `feature` / continuous aggregates, not on `ohlcv`.
6. **Ingestion is idempotent** — re-running a file never duplicates (PKs + `ON CONFLICT`).
7. **One timezone** — source is IST, store UTC `TIMESTAMPTZ` everywhere.

---

## 3. Tables at a Glance

| Table | Purpose | Key |
|---|---|---|
| `underlying` | Parents (NIFTY, RELIANCE…) | `underlying_id` |
| `instrument` | Every tradable line (parent + all options), permanent ID | `instrument_id` |
| `ohlcv` | Price bars (hypertable, big) | `(instrument_id, ts)` |
| `selection_rule` | *How* to pick options for a parent | `rule_id` |
| `option_selection` | *Which* options picked per timestamp (hypertable) | `(underlying_id, expiry, ts, rule_id, label, option_type)` |
| `feature` | Extra calc'd numbers — VWAP, greeks (hypertable) | `(instrument_id, ts)` |
| `ohlcv_5m` | Auto 5-min rollup + VWAP (continuous aggregate) | — |

**Relationships**
```
underlying (1) ──< instrument (N) ──< ohlcv, feature, (option_selection.instrument_id)
     └──< selection_rule (N) ──< option_selection (N)
```
- Parent → its options: all `instrument` rows with the same `underlying_id`. Parent line = `instrument_type INDEX`/`EQ`; options = `CE`/`PE`.
- Rule → results: `selection_rule` → `option_selection` via `rule_id`.

---

## 4. Column Reference

### `underlying`
`underlying_id` PK · `symbol` · `kind` (`INDEX`/`STOCK`) · `exchange` · `strike_step` · `lot_size` · `created_at`
Constraints: UNIQUE `(symbol, kind)`; CHECK `kind`.

### `instrument`
`instrument_id` PK · `raw_ticker` (UNIQUE, audit) · `underlying_id` FK · `instrument_type` (`INDEX`/`EQ`/`FUT`/`CE`/`PE`) · `exchange` · `expiry` · `strike` · `option_type` · `lot_size` · `is_active` · `created_at`
Constraints: FK→`underlying`; options must have expiry+strike+type, non-options must not (shape CHECK); `option_type` = `instrument_type` when set; **natural-key unique index** on `(underlying_id, instrument_type, COALESCE(expiry), COALESCE(strike), COALESCE(option_type))` prevents duplicate contracts.
Indexes: `(underlying_id, expiry, strike, option_type)` chain lookup; `(underlying_id, instrument_type)`.

### `ohlcv` (hypertable)
`instrument_id` FK · `ts` (UTC) · `open` · `high` · `low` · `close` · `volume` (default 0) · `open_interest`
PK `(instrument_id, ts)`. CHECK `high>=low`, all ≥ 0. Compress after 7 days (`segmentby=instrument_id`, `orderby=ts DESC`).

### `selection_rule`
`rule_id` PK · `underlying_id` FK · `method` (`OFFSET`/`PREMIUM_NEAR`/`PREMIUM_LTE`/`PREMIUM_GTE`/`DELTA`) · `params` JSONB · `is_active` · `created_at`

### `option_selection` (hypertable)
`underlying_id` FK · `expiry` (**always required** — which chain) · `ts` · `rule_id` FK · `label` (`ATM`, `ATM+12`, `PREM~100`…) · `option_type` · `strike` · `instrument_id` FK · `spot` · `premium` · `meta` JSONB
PK `(underlying_id, expiry, ts, rule_id, label, option_type)`.
Indexes: `(underlying_id, expiry, rule_id, label, ts)`; `(instrument_id, ts)`.

### `feature` (hypertable)
`instrument_id` FK · `ts` · `vwap` · `iv` · `delta` · `gamma` · `theta` · `vega` — add new indicators as new nullable columns.
PK `(instrument_id, ts)`.

---

## 5. Selection Subsystem

- Selection need changes: today "±15 strikes from ATM," later "premium near 100," "premium ≤ 50," "0.25 delta." All = *ways to pick which options matter now.*
- `selection_rule` = the method (config). ETL runs it per `(underlying, expiry, ts)` and writes matches to `option_selection`. Backtest reads answers only.

| Method | params | Rows (`label` / `meta`) |
|---|---|---|
| `OFFSET` | `{"range":15}` | `ATM`, `ATM±1..±15`; `meta={"offset":n}` |
| `PREMIUM_NEAR` | `{"target":100}` | `PREM~100`; `meta={"target":100,"distance":d}` |
| `PREMIUM_LTE` | `{"max":50}` | `PREM<=50#1..#n`; `meta={"premium":p}` |
| `DELTA` | `{"delta":0.25}` | `DELTA_25`; `meta={"delta":0.25}` |

**ATM** = option whose strike is closest to spot. `+n` above / `-n` below. For calls: above spot = OTM, below = ITM (puts reversed). Each `(ts,label)` stores both CE & PE rows.

**ETL correctness:**
- ATM = nearest *listed* strike (order by `abs(strike-spot)`), never `round(spot/step)*step`.
- Walk *actual* listed strikes for offsets (chains have gaps).
- `expiry` always in the key — weekly & monthly chains coexist.
- Decide reference price: cash spot vs futures (matters near expiry). Isolated in `option_selection.spot`, so it's an ETL-only change.

---

## 6. Derived Data

- **Higher timeframes / bucketed VWAP** → continuous aggregate (`ohlcv_5m`, auto-maintained). Clone pattern for 15m/1d.
- **Session (cumulative) VWAP** → window function at query time, no storage:
  `sum(close*volume) OVER w / NULLIF(sum(volume) OVER w,0)` with `w AS (ORDER BY ts)` partitioned by day.
- **Compute-once indicators** (greeks, IV) → `feature` table, join on `(instrument_id, ts)`.
- Split of duty: `option_selection` = *which* instrument; `feature` = *its* numbers.

---

## 7. Input Contract & Column Mapping

Source files use inconsistent headers (`Close` / `close_price` / `LTP`). Schema never depends on header names.

**Canonical fields** (every row resolves to these after mapping):

| Field | Required | → | Note |
|---|---|---|---|
| `ticker` | yes | `instrument.raw_ticker` | the code |
| `date` | yes | `ohlcv.ts` | |
| `time` | if intraday | `ohlcv.ts` | |
| `open`/`high`/`low`/`close` | yes | `ohlcv.*` | |
| `volume` | yes | `ohlcv.volume` | 0 if absent, never NULL |
| `open_interest` | no | `ohlcv.open_interest` | derivatives only |

**Mapping layer:** per-source profile (YAML) with alias lists → loader lowercases headers and matches.

```yaml
# source_profiles/vendorA.yaml
granularity: intraday          # intraday | daily
timezone: "Asia/Kolkata"
datetime_format: "%m/%d/%Y %H:%M:%S"
column_map:
  ticker:        [ticker, symbol, instrument, tradingsymbol, contract]
  date:          [date, trade_date, tradedate]
  time:          [time, trade_time, tradetime]
  open:          [open, open_price, o]
  high:          [high, high_price, h]
  low:           [low, low_price, l]
  close:         [close, close_price, last, ltp, c]
  volume:        [volume, vol, qty, traded_qty]
  open_interest: [open_interest, oi, openinterest]
```

**Loader behavior:** required field unmapped → **reject file**. One header matches two fields, or a field matches none → **stop, report ambiguity**. Never guess. New vendor = add a profile, no code/schema change.

---

## 8. ETL Flow

```
File (any headers)
  0. map headers → canonical fields (per-source profile)
  1. parse ticker → underlying, type, expiry, strike, option_type
  2. combine date+time → UTC
  3. validate (high≥low, ≥0)
  4. upsert underlying + instrument (natural-key)
  5. COPY bars → ohlcv
  6. selection step per (underlying, expiry, ts) → option_selection
  7. feature step (VWAP/greeks) → feature
```

Rules: bulk **`COPY`** not row inserts · `ON CONFLICT (instrument_id, ts) DO NOTHING` · reuse instrument id forever · selection runs post-load (needs spot + full chain).

---

## 9. Query Cookbook

**Setup: register NIFTY + offset rule**
```sql
INSERT INTO underlying (symbol, kind, exchange, strike_step, lot_size)
VALUES ('NIFTY','INDEX','NSE',50,25);

INSERT INTO selection_rule (underlying_id, method, params)
VALUES ((SELECT underlying_id FROM underlying WHERE symbol='NIFTY' AND kind='INDEX'),
        'OFFSET', '{"range":15}');
```

**"ATM+12 of NIFTY on a date"** (both legs)
```sql
SELECT os.ts, os.spot, os.strike, os.option_type, os.premium, os.instrument_id
FROM option_selection os
JOIN selection_rule sr ON sr.rule_id = os.rule_id
JOIN underlying u      ON u.underlying_id = os.underlying_id
WHERE u.symbol='NIFTY' AND u.kind='INDEX'
  AND os.expiry='2024-06-27'      -- REQUIRED: which chain
  AND sr.method='OFFSET' AND os.label='ATM+12'
  AND os.ts::date='2024-06-05'
ORDER BY os.ts, os.option_type;
```
Plain ATM → `label='ATM'`. Premium method → `sr.method='PREMIUM_NEAR'`, `label='PREM~100'`. Same shape.

**ATM strike direct from spot** (no selection tables)
```sql
WITH nifty AS (SELECT underlying_id FROM underlying WHERE symbol='NIFTY' AND kind='INDEX'),
spot AS (
  SELECT o.close px FROM ohlcv o JOIN instrument i USING (instrument_id)
  WHERE i.underlying_id=(SELECT underlying_id FROM nifty) AND i.instrument_type='INDEX'
    AND o.ts=TIMESTAMPTZ '2024-06-05 10:00:00+05:30')
SELECT i.strike atm_strike FROM instrument i, spot
WHERE i.underlying_id=(SELECT underlying_id FROM nifty)
  AND i.expiry='2024-06-27' AND i.option_type='CE'
ORDER BY abs(i.strike - spot.px) LIMIT 1;
```

**Full chain**
```sql
SELECT strike, option_type, instrument_id FROM instrument
WHERE underlying_id=(SELECT underlying_id FROM underlying WHERE symbol='NIFTY' AND kind='INDEX')
  AND expiry='2024-06-27' AND instrument_type IN ('CE','PE')
ORDER BY strike, option_type;
```

**Price series of a picked instrument**
```sql
SELECT ts, open, high, low, close, volume, open_interest
FROM ohlcv WHERE instrument_id=:id AND ts BETWEEN :from AND :to ORDER BY ts;
```

---

## 10. Stock Extension

Same model, one-value widening — no new tables.
- `instrument_type` CHECK already includes `EQ`.
- Insert stock as `underlying` (`kind='STOCK'`); spot line = `instrument_type='EQ'`; options = `CE`/`PE` same `underlying_id`.
- All tables/queries/rules work identically. Reference price reads the `EQ` line (vs `INDEX` line).

---

## 11. Operations

- **Chunks:** 1-day interval for intraday; tune if chunks too small/large.
- **Compression + retention:** compress `ohlcv` >7 days; add retention policy if raw ticks kept only for a window.
- **Backup:** standard PG (base backup + WAL, or logical dumps); verify chunk restore.
- **Access:** read via `asyncpg`, vectorize in `polars`/`pandas` — don't loop in SQL. Results → Mongo.
- **Integrity:** FKs + CHECKs enforce at DB layer; ingest role write, engine role read-only.

---

## 12. Full DDL

See separate file, or run the block below top-to-bottom.

```sql
-- ============================================================
--  EXTENSIONS
-- ============================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
--  1. UNDERLYING
-- ============================================================
CREATE TABLE underlying (
    underlying_id   SERIAL       PRIMARY KEY,
    symbol          TEXT         NOT NULL,
    kind            TEXT         NOT NULL,
    exchange        TEXT         NOT NULL,
    strike_step     NUMERIC(12,2),
    lot_size        INT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_underlying      UNIQUE (symbol, kind),
    CONSTRAINT ck_underlying_kind CHECK (kind IN ('INDEX','STOCK'))
);

-- ============================================================
--  2. INSTRUMENT
-- ============================================================
CREATE TABLE instrument (
    instrument_id   BIGSERIAL    PRIMARY KEY,
    raw_ticker      TEXT         NOT NULL,
    underlying_id   INT          NOT NULL,
    instrument_type TEXT         NOT NULL,
    exchange        TEXT         NOT NULL,
    expiry          DATE,
    strike          NUMERIC(12,2),
    option_type     TEXT,
    lot_size        INT,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT fk_instr_underlying
        FOREIGN KEY (underlying_id) REFERENCES underlying(underlying_id),
    CONSTRAINT uq_instr_ticker UNIQUE (raw_ticker),
    CONSTRAINT ck_instr_type
        CHECK (instrument_type IN ('INDEX','EQ','FUT','CE','PE')),
    CONSTRAINT ck_instr_option_shape CHECK (
        (instrument_type IN ('CE','PE')
             AND expiry IS NOT NULL AND strike IS NOT NULL AND option_type IS NOT NULL)
        OR
        (instrument_type IN ('INDEX','EQ','FUT')
             AND strike IS NULL AND option_type IS NULL)
    ),
    CONSTRAINT ck_instr_type_match CHECK (
        option_type IS NULL OR option_type = instrument_type
    )
);

CREATE UNIQUE INDEX uq_instr_natural ON instrument (
    underlying_id,
    instrument_type,
    COALESCE(expiry,  DATE '1900-01-01'),
    COALESCE(strike,  -1),
    COALESCE(option_type, 'X')
);
CREATE INDEX idx_instr_chain
    ON instrument (underlying_id, expiry, strike, option_type);
CREATE INDEX idx_instr_by_type
    ON instrument (underlying_id, instrument_type);

-- ============================================================
--  3. OHLCV  (hypertable)
-- ============================================================
CREATE TABLE ohlcv (
    instrument_id   BIGINT        NOT NULL,
    ts              TIMESTAMPTZ   NOT NULL,
    open            NUMERIC(14,4) NOT NULL,
    high            NUMERIC(14,4) NOT NULL,
    low             NUMERIC(14,4) NOT NULL,
    close           NUMERIC(14,4) NOT NULL,
    volume          BIGINT        NOT NULL DEFAULT 0,
    open_interest   BIGINT,
    CONSTRAINT pk_ohlcv PRIMARY KEY (instrument_id, ts),
    CONSTRAINT fk_ohlcv_instr
        FOREIGN KEY (instrument_id) REFERENCES instrument(instrument_id),
    CONSTRAINT ck_ohlcv_hl CHECK (high >= low),
    CONSTRAINT ck_ohlcv_nonneg
        CHECK (open>=0 AND high>=0 AND low>=0 AND close>=0 AND volume>=0)
);

SELECT create_hypertable('ohlcv', 'ts', chunk_time_interval => INTERVAL '1 day');

ALTER TABLE ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('ohlcv', INTERVAL '7 days');

-- ============================================================
--  4. SELECTION_RULE
-- ============================================================
CREATE TABLE selection_rule (
    rule_id         SERIAL       PRIMARY KEY,
    underlying_id   INT          NOT NULL,
    method          TEXT         NOT NULL,
    params          JSONB        NOT NULL,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT fk_rule_underlying
        FOREIGN KEY (underlying_id) REFERENCES underlying(underlying_id),
    CONSTRAINT ck_rule_method
        CHECK (method IN ('OFFSET','PREMIUM_NEAR','PREMIUM_LTE','PREMIUM_GTE','DELTA'))
);

-- ============================================================
--  5. OPTION_SELECTION  (hypertable)
-- ============================================================
CREATE TABLE option_selection (
    underlying_id   INT           NOT NULL,
    expiry          DATE          NOT NULL,
    ts              TIMESTAMPTZ   NOT NULL,
    rule_id         INT           NOT NULL,
    label           TEXT          NOT NULL,
    option_type     TEXT          NOT NULL,
    strike          NUMERIC(12,2) NOT NULL,
    instrument_id   BIGINT        NOT NULL,
    spot            NUMERIC(14,4),
    premium         NUMERIC(14,4),
    meta            JSONB,
    CONSTRAINT pk_option_selection
        PRIMARY KEY (underlying_id, expiry, ts, rule_id, label, option_type),
    CONSTRAINT fk_sel_underlying
        FOREIGN KEY (underlying_id) REFERENCES underlying(underlying_id),
    CONSTRAINT fk_sel_rule
        FOREIGN KEY (rule_id) REFERENCES selection_rule(rule_id),
    CONSTRAINT fk_sel_instr
        FOREIGN KEY (instrument_id) REFERENCES instrument(instrument_id),
    CONSTRAINT ck_sel_type CHECK (option_type IN ('CE','PE'))
);

SELECT create_hypertable('option_selection', 'ts', chunk_time_interval => INTERVAL '1 day');

CREATE INDEX idx_sel_lookup
    ON option_selection (underlying_id, expiry, rule_id, label, ts);
CREATE INDEX idx_sel_by_instr
    ON option_selection (instrument_id, ts);

-- ============================================================
--  6. FEATURE  (hypertable)
-- ============================================================
CREATE TABLE feature (
    instrument_id   BIGINT        NOT NULL,
    ts              TIMESTAMPTZ   NOT NULL,
    vwap            NUMERIC(14,4),
    iv              NUMERIC(10,6),
    delta           NUMERIC(10,6),
    gamma           NUMERIC(12,8),
    theta           NUMERIC(12,6),
    vega            NUMERIC(12,6),
    CONSTRAINT pk_feature PRIMARY KEY (instrument_id, ts),
    CONSTRAINT fk_feature_instr
        FOREIGN KEY (instrument_id) REFERENCES instrument(instrument_id)
);

SELECT create_hypertable('feature', 'ts', chunk_time_interval => INTERVAL '1 day');

-- ============================================================
--  7. CONTINUOUS AGGREGATE  (5-minute bars + VWAP)
-- ============================================================
CREATE MATERIALIZED VIEW ohlcv_5m
WITH (timescaledb.continuous) AS
SELECT
    instrument_id,
    time_bucket('5 minutes', ts) AS bucket,
    first(open, ts) AS open,
    max(high)       AS high,
    min(low)        AS low,
    last(close, ts) AS close,
    sum(volume)     AS volume,
    last(open_interest, ts) AS open_interest,
    sum(close * volume) / NULLIF(sum(volume), 0) AS vwap
FROM ohlcv
GROUP BY instrument_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_5m',
    start_offset      => INTERVAL '30 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');
```

---

*End of specification.*

