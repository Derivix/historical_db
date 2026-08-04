# 1DT directional premium-breakout backtest

An isolated backtest for the 09:22 NIFTY short-option strategy. It uses only
contracts where `expiry - trade date = 1 day`, shown in reports as `1DT`.

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

Spot, expiry, option-instrument, and intraday option-price data are cached for
the lifetime of the sweep. The database is therefore read only once per needed
contract/day, rather than once per trigger value.

## Outputs

`outputs/strategy_sdbreakout/trade_log.csv` contains every executed 1DT day
for every trigger. It includes the daily open of spot, CE, and PE; replacement
entry and exit timestamps; replacement open; directional driver; exit reason;
gross P&L; charges; and net P&L.

`outputs/strategy_sdbreakout/evaluation_parameters.csv` contains one ranked
row per trigger. It is sorted by net P&L, with `is_best_trigger=True` on the
maximum-profit trigger.
