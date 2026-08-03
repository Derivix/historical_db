"""CLI for the daily fixed-ATM NIFTY straddle minimum/maximum report."""
from __future__ import annotations

import argparse

from config.settings import BacktestConfig, SessionConfig
from db.repository import MarketDataRepository
from engine.range_backtester import AtmStraddleRangeBacktester


def parse_args():
    parser = argparse.ArgumentParser(description="NIFTY fixed-ATM straddle daily range report")
    parser.add_argument("--dsn", required=True, help="SQLAlchemy Postgres DSN")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--entry-time", default="09:30", help="ATM selection time, HH:MM")
    parser.add_argument("--exit-time", default="15:15", help="Last monitoring time, HH:MM")
    parser.add_argument("--output-dir", default="outputs")
    return parser.parse_args()


def _parse_time(value: str):
    from datetime import time
    try:
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("Time must be HH:MM") from exc


def main():
    args = parse_args()
    cfg = BacktestConfig(
        underlying_symbol=args.symbol, start_date=args.start, end_date=args.end,
        session=SessionConfig(entry_time=_parse_time(args.entry_time), exit_time=_parse_time(args.exit_time)),
        output_dir=args.output_dir,
    )
    report, paths = AtmStraddleRangeBacktester(cfg, MarketDataRepository(args.dsn)).run_and_export()
    print(report.to_string(index=False))
    if not report.empty:
        target = report["Daily Target %"].mean()
        print(f"\nTarget: {target:.2f}%")
        print(f"Stop loss (target / 2): {target / 2:.2f}%")
    print("\nReports written to:")
    for name, path in paths.items():
        print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
