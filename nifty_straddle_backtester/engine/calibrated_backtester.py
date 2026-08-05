"""Calibration followed by target/SL execution for the handwritten 09:22 strategy."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import BacktestConfig
from db.repository import MarketDataRepository
from engine.range_backtester import AtmStraddleRangeBacktester
from strategy.calibrated_straddle import simulate_calibrated_day, trade_frame


class CalibratedStraddleBacktester:
    def __init__(self, cfg: BacktestConfig, repo: MarketDataRepository):
        self.cfg, self.repo = cfg, repo
        underlying = repo.get_underlying(cfg.underlying_symbol, kind="INDEX")
        self.underlying_id = underlying["underlying_id"]
        self.strike_step = float(underlying["strike_step"])
        self.spot_instrument_id = repo.get_index_instrument_id(self.underlying_id)

    def calibrate(self) -> tuple[float, float, pd.DataFrame]:
        range_bt = AtmStraddleRangeBacktester(self.cfg, self.repo)
        report = range_bt.build_report(range_bt.run())
        if report.empty:
            raise ValueError("No valid calibration days available to compute target and stop-loss percentages.")
        target_pct = float(report["Daily Target %"].mean())
        return target_pct, target_pct / 2.0, report

    def run(
        self, target_pct: float | None = None, stop_loss_pct: float | None = None,
    ) -> dict[str, pd.DataFrame | float]:
        """Run with prior computed percentages, or calculate them when omitted."""
        if (target_pct is None) != (stop_loss_pct is None):
            raise ValueError("Provide both target_pct and stop_loss_pct, or neither.")
        if target_pct is None:
            target_pct, stop_loss_pct, calibration = self.calibrate()
        else:
            calibration = pd.DataFrame()
        trades, monitors = [], []
        for day in self.repo.get_trading_days(self.spot_instrument_id, self.cfg.start_date, self.cfg.end_date):
            trade, monitor = simulate_calibrated_day(
                day, target_pct, stop_loss_pct, self.cfg, self.repo,
                self.underlying_id, self.strike_step, self.spot_instrument_id,
            )
            if trade is not None:
                trades.append(trade)
                monitors.append(monitor)
        trade_log = trade_frame(trades)
        monitor_log = pd.concat(monitors, ignore_index=True) if monitors else pd.DataFrame()
        summary = pd.DataFrame([{
            "Calibration Days": len(calibration), "Target %": target_pct,
            "Stop Loss %": stop_loss_pct, "Backtest Trades": len(trade_log),
            "Target Exits": int((trade_log.get("exit_reason", pd.Series(dtype=str)) == "target").sum()),
            "Stop-loss Exits": int((trade_log.get("exit_reason", pd.Series(dtype=str)) == "stop_loss").sum()),
            "Time Exits": int((trade_log.get("exit_reason", pd.Series(dtype=str)) == "time_exit_15_28").sum()),
            "Gross PnL": float(trade_log["gross_pnl"].sum()) if not trade_log.empty else 0.0,
            "Charges": float(trade_log["charges"].sum()) if not trade_log.empty else 0.0,
            "Net PnL": float(trade_log["net_pnl"].sum()) if not trade_log.empty else 0.0,
        }])
        return {"target_pct": target_pct, "stop_loss_pct": stop_loss_pct, "calibration": calibration,
                "trade_log": trade_log, "monitor_log": monitor_log, "summary": summary}

    def run_and_export(
        self, target_pct: float | None = None, stop_loss_pct: float | None = None,
    ) -> tuple[dict[str, pd.DataFrame | float], dict[str, Path]]:
        result = self.run(target_pct, stop_loss_pct)
        folder = Path(self.cfg.output_dir) / "calibrated_straddle"
        folder.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name in ("calibration", "trade_log", "monitor_log", "summary"):
            frame = result[name]
            csv_path= folder / f"{name}.csv"
            frame.to_csv(csv_path, index=False)
            paths[name] = csv_path
        return result, paths
