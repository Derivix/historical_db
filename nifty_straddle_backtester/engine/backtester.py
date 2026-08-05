"""
Top-level orchestrator. Loops:  for each SL% in the sweep -> for each
trading day -> simulate_day(...). Keeps optimisation looping separate from
the single-day strategy logic in strategy/straddle.py, so multiprocessing
can later be dropped in around `run()` without touching strategy code.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - tqdm is a nicety, not a hard requirement
    def tqdm(iterable, **kwargs):
        return iterable

from config.settings import BacktestConfig
from db.repository import MarketDataRepository
from strategy.position_manager import get_position_manager
from strategy.straddle import simulate_day, TradeResult
from reporting.reports import build_trade_log, build_daily_summary, build_parameter_summary, export_reports
from reporting.plots import generate_all_plots


class Backtester:
    def __init__(self, cfg: BacktestConfig, repo: MarketDataRepository):
        self.cfg = cfg
        self.repo = repo
        self.underlying = repo.get_underlying(cfg.underlying_symbol, kind="INDEX")
        self.underlying_id = self.underlying["underlying_id"]
        # print(type(self.underlying["strike_step"]))
        self.strike_step = float(self.underlying["strike_step"])
        self.spot_instrument_id = repo.get_index_instrument_id(self.underlying_id)
        self.trading_days = repo.get_trading_days(
            self.spot_instrument_id, cfg.start_date, cfg.end_date
        )

    def run_single_sl(self, stop_loss_pct: float) -> list[TradeResult]:
        position_manager = get_position_manager(self.cfg.position_manager_name)
        results = []
        for day in self.trading_days:
            try:
                trade = simulate_day(
                    day=day, stop_loss_pct=stop_loss_pct, cfg=self.cfg, repo=self.repo,
                    underlying_id=self.underlying_id, strike_step=self.strike_step,
                    spot_instrument_id=self.spot_instrument_id,
                    position_manager=position_manager,
                )
                # print("Hello")
            except NotImplementedError:
                raise
            except Exception as e:
                print(f"[backtester] {day} @ SL {stop_loss_pct}%: skipped ({e})")
                trade = None
            if trade is not None:
                results.append(trade)
        return results

    def run_sweep(self) -> dict[float, list[TradeResult]]:
        sweep = self.cfg.sl_sweep.grid()
        out: dict[float, list[TradeResult]] = {}
        for sl_pct in tqdm(sweep, desc="SL% sweep"):
            out[sl_pct] = self.run_single_sl(sl_pct)
        return out

    def run_and_report(self, best_sl_for_plots: Optional[float] = None) -> dict:
        """Full pipeline: sweep -> trade logs -> daily summaries -> parameter summary -> plots -> export."""
        sweep_results = self.run_sweep()

        per_sl_trade_logs = {sl: build_trade_log(trades) for sl, trades in sweep_results.items()}
        parameter_summary = build_parameter_summary(per_sl_trade_logs)

        if best_sl_for_plots is None and not parameter_summary.empty:
            best_sl_for_plots = float(parameter_summary.iloc[0]["Stop Loss %"])

        best_trade_log = per_sl_trade_logs.get(best_sl_for_plots, pd.DataFrame())
        best_daily_summary = build_daily_summary(best_trade_log, best_sl_for_plots) if best_sl_for_plots else pd.DataFrame()

        # Combined trade log across every SL% (useful for ad-hoc slicing)
        all_trades_df = pd.concat(per_sl_trade_logs.values(), ignore_index=True) if per_sl_trade_logs else pd.DataFrame()

        report_paths = export_reports(
            trade_log=all_trades_df,
            daily_summary=best_daily_summary,
            parameter_summary=parameter_summary,
            output_dir=self.cfg.output_dir,
        )
        plot_paths = generate_all_plots(best_trade_log, self.cfg.output_dir) if not best_trade_log.empty else []

        return {
            "parameter_summary": parameter_summary,
            "best_sl_pct": best_sl_for_plots,
            "best_trade_log": best_trade_log,
            "best_daily_summary": best_daily_summary,
            "report_paths": report_paths,
            "plot_paths": plot_paths,
        }
