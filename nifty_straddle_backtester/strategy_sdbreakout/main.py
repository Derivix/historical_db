"""CLI for the daily short-premium breakout backtest."""
from __future__ import annotations

import argparse
import datetime as dt
from itertools import product
from pathlib import Path

import pandas as pd

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


def parse_number_grid(value: str) -> tuple[float, ...]:
    """Parse a comma-separated numeric grid, rejecting empty/non-positive values."""
    try:
        values = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Values must be comma-separated numbers, e.g. 25,35,45") from exc
    if not values or any(number <= 0 for number in values):
        raise argparse.ArgumentTypeError("Grid values must be positive numbers.")
    return tuple(dict.fromkeys(values))


def parse_time_grid(value: str) -> tuple[dt.time, ...]:
    """Parse comma-separated 24-hour times such as ``09:22,10:02``."""
    try:
        values = tuple(dt.time.fromisoformat(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Times must use HH:MM format, e.g. 09:22,10:02") from exc
    if not values:
        raise argparse.ArgumentTypeError("Provide at least one entry time.")
    return tuple(dict.fromkeys(values))


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
    parser.add_argument("--entry-times", type=parse_time_grid, help="Comma-separated entry-time grid, e.g. 09:22,10:02")
    parser.add_argument("--initial-premiums", type=parse_number_grid, help="Comma-separated initial-premium grid")
    parser.add_argument("--trigger-straddle-premiums", type=parse_number_grid, help="Comma-separated non-traded monitoring-straddle premium grid")
    parser.add_argument("--adjustment-premiums", type=parse_number_grid, help="Comma-separated replacement-premium grid")
    parser.add_argument("--replacement-target-pcts", type=parse_number_grid, help="Comma-separated replacement decay-target %% grid")
    parser.add_argument("--replacement-stop-pcts", type=parse_number_grid, help="Comma-separated replacement stop-loss %% grid")
    parser.add_argument("--portfolio-loss-limits", type=parse_number_grid, help="Comma-separated gross portfolio-loss-limit grid")
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
    sweep_requested = any((
        args.entry_times, args.initial_premiums, args.trigger_straddle_premiums, args.adjustment_premiums,
        args.replacement_target_pcts, args.replacement_stop_pcts, args.portfolio_loss_limits,
    ))
    defaults = SDBreakoutConfig()
    config_grid = product(
        args.entry_times or (defaults.entry_time,),
        args.initial_premiums or (defaults.initial_premium,),
        args.trigger_straddle_premiums or (defaults.trigger_straddle_premium,),
        args.adjustment_premiums or (defaults.adjustment_premium,),
        args.replacement_target_pcts or (defaults.replacement_target_pct,),
        args.replacement_stop_pcts or (defaults.replacement_stop_pct,),
        args.portfolio_loss_limits or (defaults.portfolio_loss_limit,),
    )
    configurations = list(config_grid)
    if len(configurations) > 500:
        raise SystemExit(f"Refusing to run {len(configurations)} static configurations; reduce the supplied grids to 500 or fewer.")
    repo = MarketDataRepository(args.dsn)
    def show_progress(day_number, total_days, day):
        print(f"Processing trading day {day_number}/{total_days}: {day}", flush=True)

    try:
        if not sweep_requested:
            cfg = SDBreakoutConfig(
                lots=args.lots, strike_search_steps=args.strike_search_steps,
                allowed_dte=args.dte, min_dte=args.min_dte, max_dte=args.max_dte,
                costs=CostConfig(enabled=not args.disable_costs),
            )
            runner = SDBreakoutBacktester(repo, args.symbol, args.start, args.end, cfg)
            log, evaluation, paths = runner.run_and_export(
                args.output_dir, trigger_values, progress_callback=show_progress,
            )
        else:
            all_logs, all_evaluations = [], []
            base_dir = Path(args.output_dir) / "strategy_sdbreakout" / "parameter_sweep"
            for config_id, values in enumerate(configurations, start=1):
                entry_time, initial_premium, monitor_premium, adjustment_premium, target_pct, stop_pct, loss_limit = values
                print(f"Running configuration {config_id}/{len(configurations)}: entry={entry_time:%H:%M}, initial={initial_premium:g}, monitor={monitor_premium:g}, adjustment={adjustment_premium:g}, target={target_pct:g}%, stop={stop_pct:g}%, loss_limit={loss_limit:g}", flush=True)
                cfg = SDBreakoutConfig(
                    entry_time=entry_time, initial_premium=initial_premium,
                    trigger_straddle_premium=monitor_premium,
                    adjustment_premium=adjustment_premium, replacement_target_pct=target_pct,
                    replacement_stop_pct=stop_pct, portfolio_loss_limit=loss_limit,
                    lots=args.lots, strike_search_steps=args.strike_search_steps,
                    allowed_dte=args.dte, min_dte=args.min_dte, max_dte=args.max_dte,
                    costs=CostConfig(enabled=not args.disable_costs),
                )
                runner = SDBreakoutBacktester(repo, args.symbol, args.start, args.end, cfg)
                run_log, run_evaluation, _ = runner.run_and_export(
                    str(base_dir / f"run_{config_id:03d}"), trigger_values, progress_callback=show_progress,
                )
                if not run_log.empty:
                    all_logs.append(run_log.assign(config_id=config_id))
                all_evaluations.append(run_evaluation.assign(config_id=config_id))

            log = pd.concat(all_logs, ignore_index=True) if all_logs else pd.DataFrame()
            evaluation = pd.concat(all_evaluations, ignore_index=True)
            evaluation = evaluation.drop(columns=["rank_by_net_pnl", "is_best_trigger"], errors="ignore")
            evaluation = evaluation.sort_values(["net_pnl", "max_drawdown"], ascending=[False, False]).reset_index(drop=True)
            evaluation.insert(0, "rank_by_net_pnl", range(1, len(evaluation) + 1))
            evaluation["is_best_configuration"] = evaluation.index == 0
            base_dir.mkdir(parents=True, exist_ok=True)
            log.to_csv(base_dir / "trade_log.csv", index=False)
            evaluation.to_csv(base_dir / "evaluation_parameters.csv", index=False)
            paths = {"trade_log": base_dir / "trade_log.csv", "evaluation_parameters": base_dir / "evaluation_parameters.csv"}
    except KeyboardInterrupt:
        print("\nBacktest cancelled. No final reports were written.")
        return
    if evaluation.empty:
        print("No trades matched the selected dates/DTE filters.")
        return
    print(evaluation.to_string(index=False))
    best = evaluation.iloc[0]
    print(f"\nBest trigger: {float(best['trigger_pct']):g}%")
    if sweep_requested:
        print(f"Best configuration: #{int(best['config_id'])} (entry={best['entry_time']}, initial={float(best['initial_premium']):g}, monitor={float(best['trigger_straddle_premium']):g}, adjustment={float(best['adjustment_premium']):g}, target={float(best['replacement_target_pct']):g}%, stop={float(best['replacement_stop_pct']):g}%, loss_limit={float(best['portfolio_loss_limit']):g})")
    print(f"Trade rows across all trading days and trigger values: {len(log)}")
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
