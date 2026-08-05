"""
Validates the full pipeline (entry -> monitor -> exit -> costs -> reports ->
plots) against synthetic data, since this sandbox has no network access to
your real TimescaleDB instance. Swap SyntheticMarketDataRepository for
MarketDataRepository(dsn) to point at real data - nothing else changes.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import BacktestConfig, StopLossSweepConfig, CostConfig, PositionSizingConfig
from engine.backtester import Backtester
from demo.synthetic_repository import SyntheticMarketDataRepository

START, END = "2024-01-01", "2024-02-29"

cfg = BacktestConfig(
    underlying_symbol="NIFTY",
    start_date=START,
    end_date=END,
    sl_sweep=StopLossSweepConfig(start_pct=10, end_pct=60, step_pct=5),  # smaller grid for a quick demo
    costs=CostConfig(enabled=True),
    sizing=PositionSizingConfig(mode="lots", lots=1),
    position_manager_name="square_off_on_trigger",
    output_dir=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs"),
)

repo = SyntheticMarketDataRepository(START, END)
bt = Backtester(cfg, repo)
result = bt.run_and_report()

print("\n=== Parameter Summary (top rows) ===")
print(result["parameter_summary"].to_string(index=False))
print(f"\nBest SL%: {result['best_sl_pct']}")
print(f"Trades in best-SL trade log: {len(result['best_trade_log'])}")
print("\nReport files:")
for k, v in result["report_paths"].items():
    print(f"  {k}: {v}")
print("\nPlot files:")
for p in result["plot_paths"]:
    print(f"  {p}")
