"""CLI: calibrate target/SL percentages, then backtest the fixed-ATM short straddle."""
from __future__ import annotations

import argparse
from datetime import time

from config.settings import BacktestConfig, CostConfig, PositionSizingConfig, SessionConfig
from db.repository import MarketDataRepository
from engine.calibrated_backtester import CalibratedStraddleBacktester


def parse_args():
    parser = argparse.ArgumentParser(description="09:22 fixed-ATM short straddle calibrated target/SL backtest")
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--start", required=True, help="Backtest start, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Backtest end, YYYY-MM-DD")
    parser.add_argument("--target-pct", type=float, help="Previously computed target percentage")
    parser.add_argument("--stop-loss-pct", type=float, help="Previously computed stop-loss percentage")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--disable-costs", action="store_true")
    parser.add_argument("--output-dir", default="outputs")
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = BacktestConfig(
        underlying_symbol=args.symbol, start_date=args.start, end_date=args.end,
        session=SessionConfig(entry_time=time(9, 22), exit_time=time(15, 25)),
        costs=CostConfig(enabled=not args.disable_costs),
        sizing=PositionSizingConfig(mode="lots", lots=args.lots), output_dir=args.output_dir,
    )
    if (args.target_pct is None) != (args.stop_loss_pct is None):
        raise SystemExit("Pass both --target-pct and --stop-loss-pct, or omit both to calibrate over --start/--end.")
    result, paths = CalibratedStraddleBacktester(cfg, MarketDataRepository(args.dsn)).run_and_export(
        args.target_pct, args.stop_loss_pct,
    )
    print(result["summary"].to_string(index=False))
    print("\nFiles written:")
    for name, path in paths.items():
        print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
