"""CLI for the daily short-premium breakout backtest."""
from __future__ import annotations

import argparse

from config.settings import CostConfig
from db.repository import MarketDataRepository
from strategy_sdbreakout.backtester import SDBreakoutBacktester, SDBreakoutConfig


def parse_dte_list(value: str) -> tuple[int, ...]:
    """Parse a comma-separated list such as ``0,1,3`` into DTE values."""
    try:
        values = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("DTE values must be comma-separated whole numbers, e.g. 0,1,2") from exc
    if not values or any(dte < 0 for dte in values):
        raise argparse.ArgumentTypeError("DTE values must be non-negative whole numbers.")
    return values


def parse_args():
    parser = argparse.ArgumentParser(description="09:22 daily short-premium breakout backtest")
    parser.add_argument("--dsn", required=True, help="SQLAlchemy PostgreSQL DSN")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--strike-search-steps", type=int, default=30)
    parser.add_argument("--trigger-start", type=float, default=3.0)
    parser.add_argument("--trigger-end", type=float, default=200.0)
    parser.add_argument("--trigger-step", type=float, default=1.0)
    parser.add_argument("--all-days", action="store_true", help="Trade every available trading day, regardless of DTE")
    parser.add_argument("--dte", type=parse_dte_list, help="Exact DTE values to trade, e.g. 0,1,3")
    parser.add_argument("--min-dte", type=int, help="Lowest DTE to trade (inclusive)")
    parser.add_argument("--max-dte", type=int, help="Highest DTE to trade (inclusive)")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--disable-costs", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.trigger_start <= 0 or args.trigger_end < args.trigger_start or args.trigger_step <= 0:
        raise SystemExit("Trigger range must be positive and have end >= start.")
    if args.min_dte is not None and args.min_dte < 0 or args.max_dte is not None and args.max_dte < 0:
        raise SystemExit("DTE bounds must be non-negative.")
    if args.min_dte is not None and args.max_dte is not None and args.min_dte > args.max_dte:
        raise SystemExit("--min-dte cannot be greater than --max-dte.")
    if args.all_days and (args.dte is not None or args.min_dte is not None or args.max_dte is not None):
        raise SystemExit("--all-days cannot be combined with --dte, --min-dte, or --max-dte.")
    count = int(round((args.trigger_end - args.trigger_start) / args.trigger_step))
    trigger_values = [round(args.trigger_start + i * args.trigger_step, 8) for i in range(count + 1)]
    cfg = SDBreakoutConfig(
        lots=args.lots,
        strike_search_steps=args.strike_search_steps,
        allowed_dte=args.dte,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        costs=CostConfig(enabled=not args.disable_costs),
    )
    runner = SDBreakoutBacktester(
        MarketDataRepository(args.dsn), args.symbol, args.start, args.end, cfg,
    )
    def show_progress(day_number, total_days, day):
        print(f"Processing trading day {day_number}/{total_days}: {day}", flush=True)

    try:
        log, evaluation, paths = runner.run_and_export(
            args.output_dir, trigger_values, progress_callback=show_progress,
        )
    except KeyboardInterrupt:
        print("\nBacktest cancelled. No final reports were written.")
        return
    if evaluation.empty:
        print("No trades matched the selected dates/DTE filters.")
        return
    print(evaluation.to_string(index=False))
    print(f"\nBest trigger: {float(evaluation.iloc[0]['trigger_pct']):g}%")
    print(f"Trade rows across all trading days and trigger values: {len(log)}")
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
