# Market Data Ingestion System

A production-grade Python system for ingesting, storing, and analysing intraday market data (NFO segment options, equity, index) into TimescaleDB.

## Quick Start

### 1. Start TimescaleDB

```bash
docker-compose up -d
```

### 2. Install dependencies

```bash
pip install -e ".[dev]"
```

### 3. Run migrations

If you want to use the remote DB configured in `.env`, just run:

```bash
market-ingest migrate
```

If your shell does not load `.env` automatically, you can also use:

```bash
python -m dotenv run -- market-ingest migrate
```

### 4. Ingest a CSV file

```bash
market-ingest ingest C:\path\to\GFDLNFO_BACKADJUSTED_02052024.csv --profile gfd
```

### 5. Compute features

```bash
market-ingest features
```

### 6. Run audit

```bash
market-ingest audit
```

### 7. Run everything in sequence

```bash
market-ingest all C:\path\to\GFDLNFO_BACKADJUSTED_02052024.csv --profile gfd
```

## Configuration

All configuration lives in `config/settings.yaml`. Override any value via environment variables prefixed with `MARKET_INGEST_` (pydantic-settings convention).

Key settings:

| Setting | Default | Description |
|---|---|---|
| `database.dsn` | `postgresql://postgres:password@localhost:5432/market_data` | PostgreSQL DSN |
| `ingest.batch_size` | `10000` | Rows per COPY batch |
| `ingest.default_profile` | `default` | Profile used when --profile not specified |
| `session.start` | `09:15` | Trading session start (IST) |
| `session.end` | `15:30` | Trading session end (IST) |
| `audit.max_gap_pct` | `5.0` | Allowable gap % before flagging |

## Source Profiles

Profiles live in `config/profiles/`. Each YAML profile describes column mapping, timezone, and datetime format for a data source.

- `default.yaml` — broad alias list for generic sources
- `gfd.yaml` — exact match for GFDLNFO CSV format

## Architecture

```
CSV/XLSX file
     │
     ▼
reader.py        ← polars batched streaming
     │
     ▼
column_mapper.py ← header normalisation via YAML profile
     │
     ▼
ticker_parser.py ← raw ticker → structured instrument fields
     │
     ▼
validator.py     ← row-level sanity checks
     │
     ▼
dao.py (COPY)    ← bulk PostgreSQL COPY
     │
     ▼
TimescaleDB      ← ohlcv hypertable
     │
     ├── vwap.py      → feature table
     ├── selection.py → option_selection hypertable
     └── checks.py    → audit_report.json
```

## Running Tests

```bash
pytest tests/ -v
```

## Database Schema

- `underlying` — index/stock hub
- `instrument` — every tradeable ticker (options, futures, spot)
- `ohlcv` — hypertable, 1-day chunks, compressed after 7 days
- `ohlcv_5m` — continuous aggregate (5-minute OHLCV + VWAP)
- `feature` — VWAP, greeks per bar
- `option_selection` — time-varying ATM/OTM labels per selection rule
- `selection_rule` — configuration for selection methods


# TimescaleDB Setup(This is for only one time setup on server)

Before running the project migrations on a new server, install and enable the **TimescaleDB** extension.

## 1. Install prerequisites

```bash
sudo apt update
sudo apt install -y gnupg wget curl lsb-release
```

## 2. Add the TimescaleDB repository

```bash
curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey | \
sudo gpg --dearmor -o /usr/share/keyrings/timescaledb-archive-keyring.gpg
```

```bash
echo "deb [signed-by=/usr/share/keyrings/timescaledb-archive-keyring.gpg] https://packagecloud.io/timescale/timescaledb/ubuntu/ noble main" | \
sudo tee /etc/apt/sources.list.d/timescaledb.list
```

## 3. Install TimescaleDB

```bash
sudo apt update
sudo apt install -y timescaledb-2-postgresql-16
```

## 4. Configure PostgreSQL

Run the TimescaleDB tuning utility:

```bash
sudo timescaledb-tune
```

Accept the recommended configuration when prompted, then restart PostgreSQL:

```bash
sudo systemctl restart postgresql
```

## 5. Enable the extension

Connect to the target database and execute:

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

You can verify the installation with:

```sql
SELECT extname, extversion
FROM pg_extension
WHERE extname = 'timescaledb';
```

## 6. Run project migrations

After the extension has been installed and enabled, run:

```bash
python -m dotenv run -- market-ingest migrate
```