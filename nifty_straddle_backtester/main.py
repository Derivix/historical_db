"""
CLI entry point.

Usage:
    python main.py --dsn "postgresql+psycopg2://user:pass@host:5432/market" \
                    --start 2024-01-01 --end 2024-12-31 \
                    --sl-start 10 --sl-end 200 --sl-step 1 \
                    --lots 1 --output-dir outputs

Reads directly from the `underlying` / `instrument` / `ohlcv` tables created
by your migrations, runs the full SL% sweep, and writes:
    outputs/trade_logs/trade_log.{csv,xlsx}
    outputs/reports/daily_summary.{csv,xlsx}
    outputs/reports/parameter_summary.{csv,xlsx}
    outputs/plots/*.png
"""
from __future__ import annotations

import argparse

from config.settings import (
    BacktestConfig, SessionConfig, StopLossSweepConfig, CostConfig, PositionSizingConfig,
)
from db.repository import MarketDataRepository
from engine.backtester import Backtester


def parse_args():
    p = argparse.ArgumentParser(description="NIFTY ATM Short Straddle Backtester")
    p.add_argument("--dsn", required=True, help="SQLAlchemy Postgres DSN")
    p.add_argument("--symbol", default="NIFTY")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--sl-start", type=float, default=10.0)
    p.add_argument("--sl-end", type=float, default=200.0)
    p.add_argument("--sl-step", type=float, default=1.0)
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--disable-costs", action="store_true")
    p.add_argument("--position-manager", default="square_off_on_trigger")
    p.add_argument("--output-dir", default="outputs")
    return p.parse_args()


def main():
    args = parse_args()

    cfg = BacktestConfig(
        underlying_symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        session=SessionConfig(),
        sl_sweep=StopLossSweepConfig(args.sl_start, args.sl_end, args.sl_step),
        costs=CostConfig(enabled=not args.disable_costs),
        sizing=PositionSizingConfig(mode="lots", lots=args.lots),
        position_manager_name=args.position_manager,
        output_dir=args.output_dir,
    )

    repo = MarketDataRepository(args.dsn)
    bt = Backtester(cfg, repo)
    result = bt.run_and_report()

    ps = result["parameter_summary"]
    print("\n=== Top 10 SL% by Net Profit & Profit Factor ===")
    print(ps.head(10).to_string(index=False))
    print(f"\nBest SL% (used for daily summary / plots): {result['best_sl_pct']}")
    print("\nReports written to:")
    for name, path in result["report_paths"].items():
        print(f"  {name}: {path}")
    print("\nPlots written to:")
    for path in result["plot_paths"]:
        print(f"  {path}")


if __name__ == "__main__":
    main()
