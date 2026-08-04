# Daily directional premium-breakout backtest

An isolated backtest for the 09:22 NIFTY short-option strategy. It tests the
rules on every available trading day, using that day's nearest complete expiry.
The resulting days-to-expiry remain visible in the reports as `dte` (for
example, `0DT`, `1DT`, or `2DT`).

## Rules

1. **09:22 entry** — sell one CE and one PE whose premiums are each closest
   to ₹20. The legs are selected independently by premium.
2. **Monitor the combined premium** from 09:23 through 15:15. The adjustment
   trigger is `initial combined premium × (1 + trigger %)`. The CLI sweeps
   every whole-number trigger from 3% through 200% by default.
3. **At the trigger**, buy back both initial legs. Determine the driver
   by comparing the CE and PE percentage increases from their own 09:22 entry:
   - PE increased more: the market is treated as moving down; sell the CE
     whose current premium is closest to ₹60.
   - CE increased more: the market is treated as moving up; sell the PE
     whose current premium is closest to ₹60.
4. **Replacement-leg exit** — buy back the ₹60 leg at the first of:
   - 60% premium decay (₹60 → ₹24 target);
   - 30% premium rise (₹60 → ₹78 stop loss);
   - gross portfolio loss of ₹3,000, including realised P&L from the initial
     pair;
   - 15:15 time exit.
5. If the selected trigger never occurs, the initial CE and PE are bought back at
   15:15. There is no lower combined-premium trigger.

The engine uses the close prices available at each one-minute timestamp. A
missing option minute uses its last known close and never looks ahead.

## Run

Run from `nifty_straddle_backtester`:

```powershell
python -m strategy_sdbreakout.main `
  --dsn "postgresql+psycopg2://USER:PASSWORD@HOST:5432/DATABASE" `
  --start 2026-01-01 `
  --end 2026-01-31 `
  --trigger-start 3 `
  --trigger-end 200 `
  --trigger-step 1
```

Optional controls: `--lots`, `--strike-search-steps`, `--disable-costs`, and
`--output-dir`. Use identical start/end values to run one trigger only.

### DTE selection

By default, the strategy trades every trading day using its nearest complete
expiry. Use one or both of these filters to control the expiry days tested:

```powershell
# Explicitly select every available trading day (any DTE)
--all-days

# Exact DTE values only (for example, expiry day, 1DTE, and 3DTE)
--dte 0,1,3

# An inclusive DTE range
--min-dte 0 --max-dte 2
```

When both are supplied, a day must pass both filters. For example,
`--dte 0,1,3 --min-dte 1 --max-dte 2` trades only `1DT`.
`--all-days` cannot be combined with a DTE filter.

Spot, expiry, option-instrument, and intraday option-price data are cached for
all trigger values for a given day. Each day is then released before the next
one is processed, keeping long all-days sweeps from accumulating the entire
date range in memory. The CLI prints the current trading day as it runs.

## Outputs

`outputs/strategy_sdbreakout/trade_log.csv` remains the complete audit log:
every executed trading day for every trigger. It includes the daily open of
spot, CE, and PE; replacement entry and exit timestamps; replacement open;
directional driver; exit reason; gross P&L; charges; and net P&L.

`outputs/strategy_sdbreakout/evaluation_parameters.csv` contains one ranked
row per trigger. It is sorted by net P&L, with `is_best_trigger=True` on the
maximum-profit trigger.

For quicker review, `outputs/strategy_sdbreakout/review/` contains a filtered
Excel workbook (`sd_breakout_review.xlsx`) and matching CSV files for the best
trigger's daily results, monthly summary, DTE summary, and the top 20 triggers.
The workbook has one tab per view, frozen headers, filters, and fitted columns.
