# Build Spec: Market-Data Ingestion System

Build a production-grade **Python data-ingestion system** that loads historical market-data Excel files (index, stock, and their options OHLCV) into a **PostgreSQL + TimescaleDB** database, plus a **data-completeness test script** that reports what's missing per instrument at the end of a run.

I will provide the Excel files in the workspace. The system must load them end-to-end and compute 1–2 sample features.

---

## Tech stack (use exactly this)

- Python 3.11+
- PostgreSQL 15+ with the TimescaleDB extension
- `psycopg` (v3) for DB access and bulk `COPY`
- `polars` for reading/transforming Excel and CSV (fall back to `pandas` + `openpyxl` only for `.xlsx` reading if polars can't)
- `pydantic` v2 for config/profile validation
- `pytest` for tests
- `PyYAML` for source profiles
- `structlog` (or stdlib `logging`) for structured logs
- Use a `pyproject.toml`, no `requirements.txt`

---

## Database schema (create via an idempotent migration script, exactly this)

Tables: `underlying`, `instrument`, `ohlcv` (hypertable), `selection_rule`, `option_selection` (hypertable), `feature` (hypertable), and a continuous aggregate `ohlcv_5m`.

- **`underlying`** — `underlying_id SERIAL PK`, `symbol TEXT`, `kind TEXT CHECK IN ('INDEX','STOCK')`, `exchange TEXT`, `strike_step NUMERIC(12,2)`, `lot_size INT`, `created_at TIMESTAMPTZ DEFAULT now()`, `UNIQUE(symbol,kind)`.
- **`instrument`** — `instrument_id BIGSERIAL PK`, `raw_ticker TEXT UNIQUE`, `underlying_id INT FK→underlying`, `instrument_type TEXT CHECK IN ('INDEX','EQ','FUT','CE','PE')`, `exchange TEXT`, `expiry DATE`, `strike NUMERIC(12,2)`, `option_type TEXT`, `lot_size INT`, `is_active BOOLEAN DEFAULT TRUE`, `created_at TIMESTAMPTZ DEFAULT now()`. Add: a **shape CHECK** (options require expiry+strike+option_type; INDEX/EQ/FUT forbid strike+option_type); a CHECK that `option_type = instrument_type` when set; a **natural-key unique index** on `(underlying_id, instrument_type, COALESCE(expiry,'1900-01-01'), COALESCE(strike,-1), COALESCE(option_type,'X'))`; indexes on `(underlying_id,expiry,strike,option_type)` and `(underlying_id,instrument_type)`.
- **`ohlcv`** — `instrument_id BIGINT FK`, `ts TIMESTAMPTZ`, `open/high/low/close NUMERIC(14,4)`, `volume BIGINT DEFAULT 0`, `open_interest BIGINT`, `PK(instrument_id,ts)`, `CHECK high>=low`, `CHECK all prices/volume >= 0`. Make it a hypertable; compression `segmentby=instrument_id orderby='ts DESC'`; compression policy after 7 days.
- **`selection_rule`** — `rule_id SERIAL PK`, `underlying_id INT FK`, `method TEXT CHECK IN ('OFFSET','PREMIUM_NEAR','PREMIUM_LTE','PREMIUM_GTE','DELTA')`, `params JSONB`, `is_active BOOLEAN DEFAULT TRUE`, `created_at TIMESTAMPTZ DEFAULT now()`.
- **`option_selection`** — `underlying_id INT FK`, `expiry DATE`, `ts TIMESTAMPTZ`, `rule_id INT FK`, `label TEXT`, `option_type TEXT CHECK IN ('CE','PE')`, `strike NUMERIC(12,2)`, `instrument_id BIGINT FK`, `spot NUMERIC(14,4)`, `premium NUMERIC(14,4)`, `meta JSONB`, `PK(underlying_id,expiry,ts,rule_id,label,option_type)`. Hypertable on `ts`; indexes `(underlying_id,expiry,rule_id,label,ts)` and `(instrument_id,ts)`.
- **`feature`** — `instrument_id BIGINT FK`, `ts TIMESTAMPTZ`, `vwap NUMERIC(14,4)`, `iv NUMERIC(10,6)`, `delta/gamma/theta/vega`, `PK(instrument_id,ts)`. Hypertable on `ts`.
- **`ohlcv_5m`** — continuous aggregate: 5-minute buckets with `first(open)`, `max(high)`, `min(low)`, `last(close)`, `sum(volume)`, `last(open_interest)`, and `vwap = sum(close*volume)/NULLIF(sum(volume),0)`, plus a refresh policy.

The migration must be safe to run repeatedly and must verify the TimescaleDB extension exists.

---

## Ticker parsing

Input tickers look like `AARTIIND27JUN24700CE.NFO` or `NIFTY27JUN2423500CE.NFO`, and equity/index spot lines like `RELIANCE.NSE` / `NIFTY.NSE`. Write a **robust parser** that extracts `(underlying_symbol, expiry, strike, option_type, segment, instrument_type)`. Handle option contracts (CE/PE), futures (FUT), and cash/index spot lines. Expiry format is `DDMMMYY` (e.g. `27JUN24`). The parser must raise a clear **typed error** on anything it can't parse — never silently mislabel. Cover it heavily with unit tests including malformed inputs.

---

## Column mapping (critical — headers vary between files)

Do **not** hardcode Excel column names. Implement a mapping layer driven by per-source YAML profiles:

```yaml
granularity: intraday          # intraday | daily
timezone: "Asia/Kolkata"
datetime_format: "%m/%d/%Y %H:%M:%S"
column_map:
  ticker:        [ticker, symbol, instrument, tradingsymbol]
  date:          [date, trade_date, tradedate]
  time:          [time, trade_time, tradetime]
  open:          [open, open_price, o]
  high:          [high, high_price, h]
  low:           [low, low_price, l]
  close:         [close, close_price, last, ltp, c]
  volume:        [volume, vol, qty, traded_qty]
  open_interest: [open_interest, oi, openinterest]
```

Canonical fields the loader resolves every row to: `ticker, date, time, open, high, low, close, volume, open_interest`. Required: `ticker, date, open, high, low, close, volume`. `time` required only when `granularity: intraday`. `open_interest` optional (derivatives only).

Rules:
- Lowercase + trim source headers before matching.
- If a required field maps to zero headers → **reject the file** with a clear error listing what's missing.
- If one header matches two canonical fields, or a field matches none → **stop and report the ambiguity**. Never guess.
- Ship a `default` profile used when a file has no explicit profile.
- Combine `date`+`time`, interpret in the profile timezone, **convert to UTC** before storing. `volume` defaults to 0, never NULL.

---

## Loading rules

- Bulk-load bars with `COPY`, never row-by-row inserts.
- Idempotent: upsert `underlying`/`instrument` via natural keys (reuse existing ids); `INSERT ... ON CONFLICT (instrument_id, ts) DO NOTHING` on `ohlcv`.
- Validate each row (high>=low, non-negative) before load; collect rejects into a report rather than aborting the whole file (configurable: `--strict` aborts).
- Process files in a streaming/batched fashion so large files don't blow memory.

---

## Sample features (implement 1–2)

After bars load, compute and populate `feature`:
1. **VWAP** per instrument per day (cumulative session VWAP) — **required**.
2. (Optional second) a simple rolling metric, e.g. 20-period moving average of close, stored in a new nullable `feature` column added via migration.

Also populate `option_selection` for at least the **OFFSET** method (±N strikes from ATM) as a worked example, following these ETL correctness rules:
- ATM = nearest **listed** strike to spot (order by `abs(strike - spot)`, **not** arithmetic rounding).
- Walk **actual listed strikes** for offsets (chains have gaps).
- `expiry` always part of the key.
- Make the offset range configurable.

---

## Data-completeness test script (key deliverable)

A standalone script (`python -m app.cli audit`) that runs **after** ingestion and prints a clear end-of-run report of what's missing, **per underlying and per option**. It must check and report:

1. **Instrument coverage** — for each underlying with options, list expiries found; for each expiry, report strikes present vs. the expected ladder (min→max by `strike_step`) and flag **missing strikes** and **missing CE/PE pairs** (a strike with a CE but no PE, or vice versa).
2. **Time-series gaps** — for each instrument, detect missing bars within its active window against the expected trading-time grid (respect `granularity`; for intraday assume a configurable session, e.g. 09:15–15:30 IST at 1-min). Report gap count and the largest gap.
3. **Missing Open Interest** — for options/futures, count/flag bars where `open_interest` is NULL.
4. **Orphans / integrity** — bars whose instrument has no parent, options whose underlying spot line is absent, selections referencing instruments with no bars.
5. **Feature coverage** — instruments/timestamps with bars but no computed `vwap`.
6. **Summary** — per-underlying pass/fail table, an overall **exit code** (non-zero if any critical check fails), a machine-readable `audit_report.json`, and a human-readable console table.

Make thresholds configurable (allowable gap %, whether missing OI is critical). The report must clearly state, at the end, **for which specific options/stocks data is missing and what kind.**

---

## Architecture (modular — organize like this)

```
project/
  pyproject.toml
  README.md
  docker-compose.yml           # TimescaleDB service for local dev/test
  config/
    settings.yaml              # db dsn, batch sizes, session hours, thresholds
    profiles/
      default.yaml
      <vendor>.yaml
  app/
    __init__.py
    config.py                  # pydantic settings + profile loader
    db/
      __init__.py
      connection.py            # psycopg pool / context managers
      migrations.py            # idempotent schema + hypertables + caggs
      dao.py                   # upserts, COPY loaders, query helpers
    ingest/
      __init__.py
      ticker_parser.py         # code → structured instrument
      column_mapper.py         # headers → canonical fields
      reader.py                # excel/csv → polars frames (batched)
      validator.py             # row sanity, reject collection
      pipeline.py              # orchestrates: read→map→parse→load
    features/
      __init__.py
      vwap.py
      selection.py             # OFFSET method (+ pluggable interface)
    audit/
      __init__.py
      checks.py                # the completeness checks
      report.py                # console + json output
    cli.py                     # entrypoints
  tests/
    conftest.py
    test_ticker_parser.py
    test_column_mapper.py
    test_validator.py
    test_pipeline.py           # end-to-end on a tiny fixture
    test_audit.py
    fixtures/
      sample_options.xlsx      # small synthetic file for tests
```

Design the selection method as a **pluggable interface** (base class + registry) so `PREMIUM_NEAR`/`DELTA` can be added later without touching the pipeline — implement only `OFFSET` now.

---

## CLI (Typer or argparse)

- `migrate` — create/upgrade schema.
- `ingest <path>` — load one file or a directory; flags `--profile`, `--strict`.
- `features` — compute VWAP + populate OFFSET selections.
- `audit` — run completeness checks, print report, write JSON, set exit code.
- `all <path>` — migrate → ingest → features → audit in sequence.

---

## Testing

- Unit tests for parser, mapper, validator (including malformed/ambiguous cases that **must raise**).
- An end-to-end pytest against a test database (use a `DATABASE_URL_TEST` env var; document a local Timescale or Docker one-liner in the README) that ingests `fixtures/sample_options.xlsx`, runs features, runs audit, and asserts the audit correctly flags a **deliberately-missing strike** and a **deliberate time gap** baked into the fixture.
- Include the `docker-compose.yml` TimescaleDB service for local dev/testing.

---

## Deliverables

1. All code, fully implemented (no `TODO`/`pass` stubs) and runnable.
2. `pyproject.toml`, `docker-compose.yml`, `config/` with a `default` profile and one example vendor profile matching my file's headers.
3. `README.md`: setup, how to run each CLI command, how profiles work, how to add a new selection method, how to read the audit report.
4. The synthetic test fixture and passing tests.

---

## Working style

- Read the Excel files I provide in the workspace **first**, and infer/propose a matching profile before writing the loader.
- State any assumptions you make about my data.
- Build incrementally and run the tests as you go.
- When done, show me the exact commands to run the full pipeline on my files.

---

## Notes for you (the builder)

- My sample data columns are: `Ticker, Date, Time, Open, High, Low, Close, Volume, Open Interest`. Date like `2/5/2024`, Time like `11:02:59`, timezone IST. Build the example vendor profile to match this exactly.
- If session hours differ from 09:15–15:30 IST 1-min, make them config-driven so gap detection stays accurate.
- Time-series gap detection is scoped to a session grid for v1 (no holiday calendar). If holiday-aware detection is needed later, integrate the `exchange_calendars` NSE calendar — leave a clear extension point.
