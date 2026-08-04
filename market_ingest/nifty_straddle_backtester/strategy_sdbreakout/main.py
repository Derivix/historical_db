"""CLI for the isolated 1DT short-premium breakout backtest."""
from __future__ import annotations

import argparse

from config.settings import CostConfig
from db.repository import MarketDataRepository
from strategy_sdbreakout.backtester import SDBreakoutBacktester, SDBreakoutConfig


def parse_args():
    parser = argparse.ArgumentParser(description="09:22 1DT short-premium breakout backtest")
    parser.add_argument("--dsn", required=True, help="SQLAlchemy PostgreSQL DSN")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--strike-search-steps", type=int, default=30)
    parser.add_argument("--trigger-start", type=float, default=3.0)
    parser.add_argument("--trigger-end", type=float, default=200.0)
    parser.add_argument("--trigger-step", type=float, default=1.0)
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--disable-costs", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.trigger_start <= 0 or args.trigger_end < args.trigger_start or args.trigger_step <= 0:
        raise SystemExit("Trigger range must be positive and have end >= start.")
    count = int(round((args.trigger_end - args.trigger_start) / args.trigger_step))
    trigger_values = [round(args.trigger_start + i * args.trigger_step, 8) for i in range(count + 1)]
    cfg = SDBreakoutConfig(
        lots=args.lots,
        strike_search_steps=args.strike_search_steps,
        costs=CostConfig(enabled=not args.disable_costs),
    )
    runner = SDBreakoutBacktester(
        MarketDataRepository(args.dsn), args.symbol, args.start, args.end, cfg,
    )
    log, evaluation, paths = runner.run_and_export(args.output_dir, trigger_values)
    print(evaluation.to_string(index=False))
    print(f"\nBest trigger: {float(evaluation.iloc[0]['trigger_pct']):g}%")
    print(f"1DT trade rows across sweep: {len(log)}")
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
