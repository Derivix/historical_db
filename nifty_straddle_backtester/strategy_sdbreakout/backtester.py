"""Backtest engine for the 09:22 short-premium breakout rules."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from engine.costs import compute_leg_cost
from utils.premium_selection import _premium_close_to
from utils.datatype import SDBreakoutConfig, _Leg, _last_price, _max_drawdown


def _max_drawdown(net_pnl: pd.Series) -> float:
    """Return the worst peak-to-trough drawdown of a chronological P&L series.

    The value is zero or negative.  The initial account peak is treated as
    zero, so a strategy that begins with a loss reports that loss as drawdown.
    """
    if net_pnl.empty:
        return 0.0
    equity = net_pnl.astype(float).cumsum()
    running_peak = equity.cummax().clip(lower=0.0)
    return float((equity - running_peak).min())


class SDBreakoutBacktester:
    """Uses the existing repository API and keeps all new behaviour in this folder."""

    def __init__(self, repo, symbol: str, start_date: str, end_date: str, cfg: SDBreakoutConfig):
        self.repo, self.symbol, self.start_date, self.end_date, self.cfg = repo, symbol, start_date, end_date, cfg
        underlying = repo.get_underlying(symbol, kind="INDEX")
        self.underlying_id = underlying["underlying_id"]
        self.strike_step = float(underlying["strike_step"])
        self.spot_instrument_id = repo.get_index_instrument_id(self.underlying_id)
        self._option_cache: dict[tuple[dt.date, float, str, dt.date], tuple[dict, pd.DataFrame] | None] = {}
        self._spot_cache: dict[dt.date, pd.DataFrame] = {}
        self._expiry_cache: dict[dt.date, dt.date | None] = {}

    def _active_expiry(self, day: dt.date) -> dt.date | None:
        """Return the nearest complete expiry available on a trading day.

        The original version only traded when the next calendar day was an
        expiry (``1DT``).  That omitted the majority of trading days.  Using
        the active, nearest expiry makes the same intraday rules testable on
        every day while retaining the DTE in the output for analysis.
        """
        if day not in self._expiry_cache:
            self._expiry_cache[day] = self.repo.find_nearest_complete_expiry(
                self.underlying_id, str(day), str(day + dt.timedelta(days=45)),
            )
        return self._expiry_cache[day]

    def _is_selected_dte(self, day: dt.date, expiry: dt.date | None) -> bool:
        """Apply optional exact-DTE and DTE-range filters to a trading day."""
        if expiry is None:
            return False
        dte = (expiry - day).days
        if self.cfg.allowed_dte is not None and dte not in self.cfg.allowed_dte:
            return False
        if self.cfg.min_dte is not None and dte < self.cfg.min_dte:
            return False
        return self.cfg.max_dte is None or dte <= self.cfg.max_dte

    def _spot_data(self, day: dt.date) -> pd.DataFrame:
        if day not in self._spot_cache:
            self._spot_cache[day] = self.repo.get_spot_ohlcv(
                self.spot_instrument_id, str(day), str(day),
            ).sort_values("ts")
        return self._spot_cache[day]

    

    @staticmethod
    def _price(leg: _Leg, when: dt.datetime) -> float | None:
        return _last_price(leg.prices, when)

    def _charges(self, legs: list[_Leg]) -> float:
        total = 0.0
        for leg in legs:
            if leg.exit_price is None:
                continue
            total += compute_leg_cost(leg.entry_price, leg.quantity, "SELL", self.cfg.costs).total
            total += compute_leg_cost(leg.exit_price, leg.quantity, "BUY", self.cfg.costs).total
        return total

    def simulate_day(self, day: dt.date, trigger_pct: float) -> dict | None:
        expiry = self._active_expiry(day)
        if not self._is_selected_dte(day, expiry):
            return None
        entry_dt, forced_exit = (dt.datetime.combine(day, self.cfg.entry_time), dt.datetime.combine(day, self.cfg.exit_time))
        spot = self._spot_data(day)
        session = spot[(spot["ts"] >= entry_dt) & (spot["ts"] <= forced_exit)].copy()
        if session.empty:
            return None
        entry_spot = _last_price(session, entry_dt)
        if entry_spot is None:
            return None
        ce = _premium_close_to(
            self.repo, 
            self._option_cache, 
            self.underlying_id, 
            day, 
            expiry, 
            "CE",
            entry_dt, 
            entry_spot, 
            self.cfg.initial_premium, 
            self.cfg.strike_step, 
            self.cfg
            )
        pe = _premium_close_to(
            self.repo, 
            self._option_cache, 
            self.underlying_id, 
            day, expiry, 
            "PE", 
            entry_dt, 
            entry_spot, 
            self.cfg.initial_premium, 
            self.cfg.strike_step, 
            self.cfg
            )
        monitor_ce = _premium_close_to(self.repo, self._option_cache, self.underlying_id, day, expiry, "CE", entry_dt, entry_spot, self.cfg.trigger_straddle_premium, self.cfg.strike_step, self.cfg)
        monitor_pe = _premium_close_to(self.repo, self._option_cache, self.underlying_id, day, expiry, "PE", entry_dt, entry_spot, self.cfg.trigger_straddle_premium, self.cfg.strike_step, self.cfg)
        if ce is None or pe is None or monitor_ce is None or monitor_pe is None:
            return None
        initial = [ce, pe]
        all_legs = [ce, pe]
        initial_value = ce.entry_price + pe.entry_price
        monitor_initial_value = monitor_ce.entry_price + monitor_pe.entry_price
        up_trigger = monitor_initial_value * (1 + trigger_pct / 100)
        replacement: _Leg | None = None
        adjustment_reason = "none"
        directional_driver = ""
        exit_reason = "time_exit_initial"
        exit_dt = forced_exit

        # Do not re-use the entry candle as a breakout signal.
        for _, row in session[session["ts"] > entry_dt].iterrows():
            when, spot_px = row["ts"], float(row["close"])
            if replacement is None:
                monitor_ce_px = self._price(monitor_ce, when)
                monitor_pe_px = self._price(monitor_pe, when)
                if monitor_ce_px is None or monitor_pe_px is None:
                    continue
                current_value = monitor_ce_px + monitor_pe_px
                # The separate monitoring straddle creates the signal but is
                # never traded. A CE-leading move signals an up-move, so sell
                # PE; a PE-leading move signals a down-move, so sell CE.
                ce_change_pct = (monitor_ce_px / monitor_ce.entry_price - 1) * 100
                pe_change_pct = (monitor_pe_px / monitor_pe.entry_price - 1) * 100
                driver, replacement_type = (
                    ("CE", "PE") if ce_change_pct >= pe_change_pct else ("PE", "CE")
                )
                directional_driver = driver
                if current_value >= up_trigger:
                    # The monitor creates the signal; the original short
                    # straddle is the position that must be closed.
                    ce_px, pe_px = self._price(ce, when), self._price(pe, when)
                    if ce_px is None or pe_px is None:
                        continue
                    ce.close(when, ce_px); pe.close(when, pe_px)
                    adjustment_reason = f"straddle_up_{trigger_pct:g}pct_{driver}_driver_sell_{replacement_type.lower()}"
                else:
                    continue
                replacement = self._premium_close_to(self.repo, self._option_cache, self.underlying_id, day, expiry, replacement_type, when, spot_px, self.cfg.adjustment_premium, self.cfg.strike_step, self.cfg)
                if replacement is None:
                    exit_reason, exit_dt = "adjustment_no_replacement", when
                    break
                all_legs.append(replacement)
                # The portfolio limit applies once leg 3/4 is active.  The new leg's P&L is zero at entry.
                if sum(leg.gross_pnl for leg in initial) <= -self.cfg.portfolio_loss_limit:
                    replacement.close(when, replacement.entry_price)
                    exit_reason, exit_dt = "portfolio_stop_loss", when
                    break
                continue

            replacement_px = self._price(replacement, when)
            if replacement_px is None:
                continue
            portfolio_gross = sum(leg.gross_pnl for leg in initial) + (replacement.entry_price - replacement_px) * replacement.quantity
            if replacement_px <= replacement.entry_price * (1 - self.cfg.replacement_target_pct / 100):
                replacement.close(when, replacement_px); exit_reason, exit_dt = "replacement_target_60pct", when; break
            if replacement_px >= replacement.entry_price * (1 + self.cfg.replacement_stop_pct / 100):
                replacement.close(when, replacement_px); exit_reason, exit_dt = "replacement_stop_loss_30pct", when; break
            if portfolio_gross <= -self.cfg.portfolio_loss_limit:
                replacement.close(when, replacement_px); exit_reason, exit_dt = "portfolio_stop_loss", when; break
        else:
            # The loop completed without an exit: close whatever is still open at the 15:15 price.
            for leg in all_legs:
                if leg.exit_price is None:
                    px = self._price(leg, forced_exit)
                    leg.close(forced_exit, leg.entry_price if px is None else px)

        # A break can leave the initial pair open only if the data disappeared before a signal; close safely.
        for leg in all_legs:
            if leg.exit_price is None:
                px = self._price(leg, exit_dt)
                leg.close(exit_dt, leg.entry_price if px is None else px)
        gross = sum(leg.gross_pnl for leg in all_legs)
        charges = self._charges(all_legs)
        return {
            "trigger_pct": trigger_pct, "dte": f"{(expiry - day).days}DT", "date": day, "expiry": expiry,
            "entry_time": entry_dt, "exit_time": exit_dt, "entry_spot": entry_spot,
            "spot_open": float(session.iloc[0]["open"]),
            "initial_ce_ticker": ce.ticker, "initial_ce_strike": ce.strike, "initial_ce_entry": ce.entry_price, "initial_ce_exit": ce.exit_price,
            "initial_ce_open": float(ce.prices.iloc[0]["open"]),
            "initial_pe_ticker": pe.ticker, "initial_pe_strike": pe.strike, "initial_pe_entry": pe.entry_price, "initial_pe_exit": pe.exit_price,
            "initial_pe_open": float(pe.prices.iloc[0]["open"]),
            "initial_combined_premium": initial_value,
            "monitor_ce_ticker": monitor_ce.ticker, "monitor_ce_strike": monitor_ce.strike, "monitor_ce_entry": monitor_ce.entry_price,
            "monitor_pe_ticker": monitor_pe.ticker, "monitor_pe_strike": monitor_pe.strike, "monitor_pe_entry": monitor_pe.entry_price,
            "monitor_combined_premium": monitor_initial_value, "straddle_up_trigger": up_trigger,
            "adjustment_reason": adjustment_reason,
            "directional_driver": directional_driver,
            "replacement_type": replacement.option_type if replacement else "", "replacement_ticker": replacement.ticker if replacement else "",
            "replacement_strike": replacement.strike if replacement else None, "replacement_entry": replacement.entry_price if replacement else None,
            "replacement_exit": replacement.exit_price if replacement else None,
            "replacement_entry_time": replacement.entry_time if replacement else None,
            "replacement_exit_time": replacement.exit_time if replacement else None,
            "replacement_open": float(replacement.prices.iloc[0]["open"]) if replacement else None,
            "replacement_target": replacement.entry_price * .4 if replacement else None,
            "replacement_stop": replacement.entry_price * 1.3 if replacement else None,
            "exit_reason": exit_reason, "quantity": ce.quantity, "gross_pnl": gross, "charges": charges, "net_pnl": gross - charges,
        }

    def _discard_day_cache(self, day: dt.date) -> None:
        """Release intraday frames after every trigger has used this day."""
        self._spot_cache.pop(day, None)
        for key in [key for key in self._option_cache if key[-1] == day]:
            del self._option_cache[key]

    def run_and_export(
        self, output_dir: str, trigger_values: list[float], progress_callback=None,
    ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Path]]:
        days = self.repo.get_trading_days(self.spot_instrument_id, self.start_date, self.end_date)
        eligible_days = sum(self._active_expiry(day) is not None for day in days)
        selected_days = sum(
            self._is_selected_dte(day, self._active_expiry(day)) for day in days
        )
        # Process all triggers for one day before moving forward.  This keeps
        # database reads shared by the sweep but avoids retaining every
        # option-chain minute frame for the full backtest period.
        trades_by_trigger: dict[float, list[dict]] = {trigger_pct: [] for trigger_pct in trigger_values}
        for day_number, day in enumerate(days, start=1):
            if progress_callback is not None:
                progress_callback(day_number, len(days), day)
            try:
                for trigger_pct in trigger_values:
                    result = self.simulate_day(day, trigger_pct)
                    if result is not None:
                        trades_by_trigger[trigger_pct].append(result)
            finally:
                self._discard_day_cache(day)
        logs = [pd.DataFrame(trades) for trades in trades_by_trigger.values() if trades]
        log = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()
        if not log.empty:
            log = log.sort_values(["trigger_pct", "date"]).reset_index(drop=True)
        parameter_rows = []
        base_parameters = asdict(self.cfg)
        base_parameters.pop("costs")
        base_parameters.update({f"cost_{key}": value for key, value in asdict(self.cfg.costs).items()})
        for trigger_pct in trigger_values:
            subset = log[log["trigger_pct"] == trigger_pct] if not log.empty else pd.DataFrame()
            parameter_rows.append(base_parameters | {
                "trigger_pct": trigger_pct, "trading_days": len(days), "eligible_days": eligible_days,
                "selected_dte_days": selected_days,
                "backtest_trades": len(subset),
                "net_pnl": float(subset["net_pnl"].sum()) if not subset.empty else 0.0,
                "gross_pnl": float(subset["gross_pnl"].sum()) if not subset.empty else 0.0,
                "charges": float(subset["charges"].sum()) if not subset.empty else 0.0,
                "win_rate_pct": float((subset["net_pnl"] > 0).mean() * 100) if not subset.empty else 0.0,
                "max_daily_loss": float(subset["net_pnl"].min()) if not subset.empty else 0.0,
                "max_drawdown": _max_drawdown(subset["net_pnl"]) if not subset.empty else 0.0,
                "adjustments": int((subset["adjustment_reason"] != "none").sum()) if not subset.empty else 0,
            })
        evaluation = pd.DataFrame(parameter_rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)
        evaluation.insert(0, "rank_by_net_pnl", range(1, len(evaluation) + 1))
        evaluation.insert(1, "is_best_trigger", evaluation.index == 0)
        folder = Path(output_dir) / "strategy_sdbreakout"
        folder.mkdir(parents=True, exist_ok=True)
        log_path, evaluation_path = folder / "trade_log.csv", folder / "evaluation_parameters.csv"
        log.to_csv(log_path, index=False); evaluation.to_csv(evaluation_path, index=False)
        review_paths = self._export_review_pack(folder, log, evaluation)
        return log, evaluation, {
            "trade_log": log_path, "evaluation_parameters": evaluation_path, **review_paths,
        }

    @staticmethod
    def _export_review_pack(folder: Path, log: pd.DataFrame, evaluation: pd.DataFrame) -> dict[str, Path]:
        """Export concise, human-reviewable reports without losing the full log."""
        review_folder = folder / "review"
        review_folder.mkdir(exist_ok=True)
        if log.empty or evaluation.empty:
            return {}

        best_trigger = float(evaluation.iloc[0]["trigger_pct"])
        best_daily = log[log["trigger_pct"] == best_trigger].sort_values("date").copy()
        best_daily.insert(0, "running_net_pnl", best_daily["net_pnl"].cumsum())
        best_daily.insert(1, "trade_number", range(1, len(best_daily) + 1))
        best_daily.insert(2, "running_peak_net_pnl", best_daily["running_net_pnl"].cummax().clip(lower=0.0))
        best_daily.insert(3, "drawdown", best_daily["running_net_pnl"] - best_daily["running_peak_net_pnl"])

        monthly = best_daily.assign(month=pd.to_datetime(best_daily["date"]).dt.to_period("M").astype(str)).groupby("month", as_index=False).agg(
            trades=("date", "size"), net_pnl=("net_pnl", "sum"), gross_pnl=("gross_pnl", "sum"),
            charges=("charges", "sum"), win_rate_pct=("net_pnl", lambda values: (values > 0).mean() * 100),
            worst_day_pnl=("net_pnl", "min"), best_day_pnl=("net_pnl", "max"), max_drawdown=("net_pnl", _max_drawdown),
        )
        dte = best_daily.groupby("dte", as_index=False).agg(
            trades=("date", "size"), net_pnl=("net_pnl", "sum"), gross_pnl=("gross_pnl", "sum"),
            charges=("charges", "sum"), win_rate_pct=("net_pnl", lambda values: (values > 0).mean() * 100),
            worst_day_pnl=("net_pnl", "min"), best_day_pnl=("net_pnl", "max"), max_drawdown=("net_pnl", _max_drawdown),
        )
        dte["dte_days"] = dte["dte"].str.replace("DT", "", regex=False).astype(int)
        dte = dte.sort_values("dte_days").drop(columns="dte_days")

        top_triggers = evaluation.head(20).copy()
        daily_path = review_folder / "best_trigger_daily_results.csv"
        monthly_path = review_folder / "best_trigger_monthly_summary.csv"
        dte_path = review_folder / "best_trigger_dte_summary.csv"
        top_path = review_folder / "top_20_trigger_comparison.csv"
        best_daily.to_csv(daily_path, index=False)
        monthly.to_csv(monthly_path, index=False)
        dte.to_csv(dte_path, index=False)
        top_triggers.to_csv(top_path, index=False)

        workbook_path = review_folder / "sd_breakout_review.xlsx"
        with pd.ExcelWriter(workbook_path, engine="openpyxl", datetime_format="yyyy-mm-dd hh:mm") as writer:
            sheets = {
                "Best daily results": best_daily,
                "Monthly summary": monthly,
                "DTE summary": dte,
                "Top 20 triggers": top_triggers,
            }
            for name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=name, index=False)
                worksheet = writer.sheets[name]
                worksheet.freeze_panes = "A2"
                worksheet.auto_filter.ref = worksheet.dimensions
                for column_cells in worksheet.columns:
                    width = min(max(len(str(cell.value or "")) for cell in column_cells) + 2, 32)
                    worksheet.column_dimensions[column_cells[0].column_letter].width = width

        return {
            "review_workbook": workbook_path,
            "best_trigger_daily_results": daily_path,
            "best_trigger_monthly_summary": monthly_path,
            "best_trigger_dte_summary": dte_path,
            "top_20_trigger_comparison": top_path,
        }
