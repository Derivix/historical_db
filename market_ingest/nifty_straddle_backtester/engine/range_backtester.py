"""Runner and report exporter for the fixed-ATM daily straddle range strategy."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from config.settings import BacktestConfig
from db.repository import MarketDataRepository
from strategy.atm_straddle_range import DailyStraddleRange, measure_daily_atm_straddle_range


class AtmStraddleRangeBacktester:
    def __init__(self, cfg: BacktestConfig, repo: MarketDataRepository):
        self.cfg = cfg
        self.repo = repo
        self.underlying = repo.get_underlying(cfg.underlying_symbol, kind="INDEX")
        self.underlying_id = self.underlying["underlying_id"]
        self.strike_step = float(self.underlying["strike_step"])
        self.spot_instrument_id = repo.get_index_instrument_id(self.underlying_id)
        self.trading_days = repo.get_trading_days(
            self.spot_instrument_id, cfg.start_date, cfg.end_date
        )

    def run(self) -> list[DailyStraddleRange]:
        results = []
        for day in self.trading_days:
            try:
                result = measure_daily_atm_straddle_range(
                    day, self.cfg, self.repo, self.underlying_id,
                    self.strike_step, self.spot_instrument_id,
                )
            except Exception as exc:
                print(f"[atm-straddle-range] {day}: skipped ({exc})")
                result = None
            if result is not None:
                results.append(result)
        return results

    @staticmethod
    def build_report(results: list[DailyStraddleRange]) -> pd.DataFrame:
        columns = [
            "Date", "Expiry", "ATM Strike", "Entry Time", "Entry Spot", "CE Entry",
            "PE Entry", "Entry Straddle Value", "Exit Time", "CE Exit", "PE Exit",
            "Exit Straddle Value", "Min Straddle Value", "Min Time",
            "Max Straddle Value", "Max Time", "Range", "Daily Target %", "Observations",
        ]
        if not results:
            return pd.DataFrame(columns=columns)
        report = pd.DataFrame(asdict(result) for result in results).rename(columns={
            "date": "Date", "expiry": "Expiry", "strike": "ATM Strike",
            "entry_time": "Entry Time", "entry_spot": "Entry Spot", "ce_entry": "CE Entry",
            "pe_entry": "PE Entry", "entry_straddle_value": "Entry Straddle Value",
            "exit_time": "Exit Time", "ce_exit": "CE Exit", "pe_exit": "PE Exit",
            "exit_straddle_value": "Exit Straddle Value",
            "min_straddle_value": "Min Straddle Value", "min_time": "Min Time",
            "max_straddle_value": "Max Straddle Value", "max_time": "Max Time",
            "observations": "Observations",
        })
        report["Range"] = report["Max Straddle Value"] - report["Min Straddle Value"]
        report["Daily Target %"] = (
            (report["Entry Straddle Value"] - report["Min Straddle Value"])
            / report["Entry Straddle Value"]
            * 100
        )
        return report[columns].sort_values("Date").reset_index(drop=True)

    @staticmethod
    def build_target_summary(report: pd.DataFrame) -> pd.DataFrame:
        """Average daily straddle decay and derive the requested half-target SL."""
        if report.empty:
            return pd.DataFrame(columns=["Trading Days", "Target %", "Stop Loss %"])

        target_pct = float(report["Daily Target %"].mean())
        return pd.DataFrame([{
            "Trading Days": len(report),
            "Target %": target_pct,
            "Stop Loss %": target_pct / 2,
        }])

    def run_and_export(self) -> tuple[pd.DataFrame, dict[str, Path]]:
        report = self.build_report(self.run())
        target_summary = self.build_target_summary(report)
        output_dir = Path(self.cfg.output_dir) / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = output_dir / "daily_atm_straddle_range.csv"
        # xlsx_path = output_dir / "daily_atm_straddle_range.xlsx"
        target_csv_path = output_dir / "atm_straddle_target_summary.csv"
        # target_xlsx_path = output_dir / "atm_straddle_target_summary.xlsx"
        report.to_csv(csv_path, index=False)
        # report.to_excel(xlsx_path, index=False)
        target_summary.to_csv(target_csv_path, index=False)
        # target_summary.to_excel(target_xlsx_path, index=False)
        return report, {
            "csv": csv_path,
            "target_csv": target_csv_path,
        }
