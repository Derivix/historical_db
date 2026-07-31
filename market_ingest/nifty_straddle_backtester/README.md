# NIFTY ATM Short Straddle Backtester

Backtesting framework for **Strategy 1 - NIFTY ATM Short Straddle with
Spot-Based Dynamic Trend Management**, built against your existing
TimescaleDB schema (`underlying` / `instrument` / `ohlcv` tables from
`market-ingest migrate`).

## What it does

1. At 09:30, reads NIFTY spot, resolves the ATM strike, sells 1 lot of the
   ATM CE + PE (short straddle).
2. Computes a spot-based SL trigger band: `entry_spot ± (combined_premium * SL%)`.
3. Monitors every 1-min candle until 15:15, exiting when spot crosses either
   trigger (or at session close if it never does).
4. Sweeps SL% from 10% to 200% (configurable step), independently, and
   ranks the results.
5. Produces a trade log, daily summary, and parameter summary
   (CSV + Excel), plus 9 chart types (equity curve, drawdown, monthly
   heatmap, daily PnL, histograms, streaks, etc).

## Project layout

```
config/settings.py         Dataclasses for session times, SL sweep, costs, sizing
db/repository.py            All SQL against underlying/instrument/ohlcv - only file that talks to Postgres
strategy/straddle.py         Entry, ATM resolution, trigger calc, single-day simulation
strategy/position_manager.py Pluggable exit/management logic (exit-both is implemented;
                              exit-one-leg / trail / shift-strike / re-entry / hedge are
                              stubbed for you to fill in - see class docstrings)
engine/costs.py              Brokerage / STT / exchange / GST / stamp duty / slippage model
engine/backtester.py         Orchestrates the SL% sweep across all trading days
reporting/metrics.py          Sharpe, Sortino, Calmar, drawdown, streaks, profit factor, etc.
reporting/reports.py          Builds + exports trade log / daily summary / parameter summary
reporting/plots.py            All 9 chart types
demo/synthetic_repository.py  In-memory fake data generator (no DB needed) - used to validate
demo/demo_run.py               the full pipeline end-to-end; this is what was run to produce
                                the sample outputs/ folder in this delivery.
main.py                       CLI entry point for your REAL database
```

## Running against your real database

```bash
pip install -r requirements.txt

python main.py \
    --dsn "postgresql+psycopg2://user:password@host:5432/market" \
    --symbol NIFTY \
    --start 2024-01-01 --end 2024-12-31 \
    --sl-start 10 --sl-end 200 --sl-step 1 \
    --lots 1 \
    --output-dir outputs
```

Requirements on your DB side (matches your migrations file exactly):
- `underlying` row for NIFTY/INDEX with a non-null `strike_step`.
- An `instrument` row with `instrument_type='INDEX'` carrying the spot OHLCV.
- `instrument` rows with `instrument_type IN ('CE','PE')` for every strike/expiry you need.
- `ohlcv` populated at 1-minute resolution for both spot and options.

## Running the offline demo (no DB required)

```bash
python demo/demo_run.py
```

This regenerates synthetic NIFTY spot + option-chain data in memory and runs
the identical pipeline (`Backtester` doesn't know or care whether it's
talking to Postgres or the synthetic repo - both implement the same
duck-typed interface). Useful for CI / sanity checks / onboarding, and is
exactly what was used to produce the sample files under `outputs/` in this
delivery, since this environment doesn't have network access to your
Postgres instance.

## Extending position management

Every management style beyond "exit both legs at first trigger" (exit one
leg, trailing stop, strike-shift, re-entry, hedge) is intentionally left as
a stub class in `strategy/position_manager.py`, per your spec's note that
you'll provide those rules separately. Implement `decide()` on the relevant
class and register it - `engine/backtester.py` and `strategy/straddle.py`
never need to change.

## Adding new strategies (Iron Fly, Iron Condor, Strangle, ...)

`strategy/straddle.py`'s `simulate_day()` is the only strategy-specific
file. A new strategy = a new `simulate_<name>_day()` function reusing
`db/repository.py`, `engine/costs.py`, `strategy/position_manager.py`, and
`reporting/*` unchanged.

## Daily fixed-ATM straddle range

`range_main.py` implements a separate observational strategy. At the entry
time it reads NIFTY spot, selects the nearest ATM strike and the nearest
available expiry, then keeps that **same CE + PE pair** for the rest of the
session. It reports the date, selected strike, entry and exit CE/PE values,
and the minimum and maximum combined premium with their timestamps for every
trading day. It does not place simulated trades, apply stop losses, or shift
strikes.

```bash
python range_main.py \
    --dsn "postgresql+psycopg2://user:password@host:5432/market" \
    --symbol NIFTY \
    --start 2024-01-01 --end 2024-12-31 \
    --entry-time 09:30 --exit-time 15:15 \
    --output-dir outputs
```

The command writes `outputs/reports/daily_atm_straddle_range.csv` and `.xlsx`.

## Notes on the cost model

All values in `config/settings.py::CostConfig` are illustrative NSE-style
defaults - override them for your actual broker's brokerage/STT/GST rates.
Set `costs=CostConfig(enabled=False)` for a gross-PnL run.
