"""Fixed-ATM short-straddle simulation using precomputed target and SL percentages."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Optional

import pandas as pd

from config.settings import BacktestConfig
from db.repository import MarketDataRepository
from engine.costs import total_trade_cost
from strategy.straddle import resolve_atm_strike, resolve_lot_quantity


@dataclass(frozen=True)
class CalibratedTrade:
    dte: str
    date: dt.date
    expiry: dt.date
    strike: float
    entry_time: dt.datetime
    exit_time: dt.datetime
    spot_entry: float
    spot_exit: float
    ce_entry: float
    pe_entry: float
    ce_exit: float
    pe_exit: float
    entry_straddle_value: float
    exit_straddle_value: float
    target_pct: float
    stop_loss_pct: float
    target_straddle_value: float
    stop_loss_straddle_value: float
    exit_reason: str
    quantity: int
    gross_pnl: float
    charges: float
    net_pnl: float


def _last_on_or_before(df: pd.DataFrame, when: dt.datetime) -> Optional[pd.Series]:
    eligible = df[df["ts"] <= when]
    return None if eligible.empty else eligible.iloc[-1]


def _monitor_prices(spot_df: pd.DataFrame, ce_df: pd.DataFrame, pe_df: pd.DataFrame) -> pd.DataFrame:
    """Align option closes to each spot minute without looking ahead."""
    monitor = spot_df[["ts", "close"]].rename(columns={"close": "spot_close"}).sort_values("ts")
    ce = ce_df[["ts", "close"]].rename(columns={"close": "ce_close"}).sort_values("ts")
    pe = pe_df[["ts", "close"]].rename(columns={"close": "pe_close"}).sort_values("ts")
    monitor = pd.merge_asof(monitor, ce, on="ts", direction="backward")
    monitor = pd.merge_asof(monitor, pe, on="ts", direction="backward")
    monitor = monitor.dropna(subset=["ce_close", "pe_close"]).copy()
    monitor["straddle_value"] = monitor["ce_close"].astype(float) + monitor["pe_close"].astype(float)
    return monitor


def simulate_calibrated_day(
    day: dt.date,
    target_pct: float,
    stop_loss_pct: float,
    cfg: BacktestConfig,
    repo: MarketDataRepository,
    underlying_id: int,
    strike_step: float,
    spot_instrument_id: int,
) -> tuple[Optional[CalibratedTrade], pd.DataFrame]:
    """Sell the 09:22 ATM straddle; monitor from 09:23 and exit on premium thresholds."""
    entry_dt = dt.datetime.combine(day, cfg.session.entry_time)
    monitor_start = entry_dt + dt.timedelta(minutes=1)
    forced_exit_dt = dt.datetime.combine(day, cfg.session.exit_time)
    spot_df = repo.get_spot_ohlcv(spot_instrument_id, str(day), str(day))
    session = spot_df[(spot_df["ts"] >= entry_dt) & (spot_df["ts"] <= forced_exit_dt)].copy()
    if session.empty:
        return None, pd.DataFrame()

    entry_spot_row = _last_on_or_before(session, entry_dt)
    if entry_spot_row is None:
        return None, pd.DataFrame()
    strike = resolve_atm_strike(float(entry_spot_row["close"]), strike_step)
    expiry = repo.find_nearest_complete_expiry(underlying_id, str(day), str(day + dt.timedelta(days=45)))
    if expiry is None:
        return None, pd.DataFrame()
    ce_inst = repo.get_option_instrument(underlying_id, expiry, strike, "CE")
    pe_inst = repo.get_option_instrument(underlying_id, expiry, strike, "PE")
    if ce_inst is None or pe_inst is None:
        return None, pd.DataFrame()
    ce_df = repo.get_option_ohlcv(ce_inst["instrument_id"], day)
    pe_df = repo.get_option_ohlcv(pe_inst["instrument_id"], day)
    if ce_df.empty or pe_df.empty:
        return None, pd.DataFrame()

    ce_entry = _last_on_or_before(ce_df, entry_dt)
    pe_entry = _last_on_or_before(pe_df, entry_dt)
    if ce_entry is None or pe_entry is None:
        return None, pd.DataFrame()
    ce_entry_px, pe_entry_px = float(ce_entry["close"]), float(pe_entry["close"])
    entry_value = ce_entry_px + pe_entry_px
    target_value = entry_value * (1 - target_pct / 100.0)
    stop_value = entry_value * (1 + stop_loss_pct / 100.0)

    monitor = _monitor_prices(session, ce_df, pe_df)
    monitor = monitor[(monitor["ts"] >= monitor_start) & (monitor["ts"] <= forced_exit_dt)].copy()
    if monitor.empty:
        return None, pd.DataFrame()
    monitor["date"] = day
    monitor["expiry"] = expiry
    monitor["strike"] = strike
    monitor["entry_straddle_value"] = entry_value
    monitor["target_pct"] = target_pct
    monitor["stop_loss_pct"] = stop_loss_pct
    monitor["target_straddle_value"] = target_value
    monitor["stop_loss_straddle_value"] = stop_value
    monitor["target_hit"] = monitor["straddle_value"] <= target_value
    monitor["stop_loss_hit"] = monitor["straddle_value"] >= stop_value

    hit = monitor[monitor["target_hit"] | monitor["stop_loss_hit"]]
    if hit.empty:
        exit_row, reason = monitor.iloc[-1], "time_exit_15_28"
    else:
        exit_row = hit.iloc[0]
        # A close cannot normally hit both; use SL conservatively if it does.
        reason = "stop_loss" if bool(exit_row["stop_loss_hit"]) else "target"
    monitor["is_exit"] = monitor["ts"] == exit_row["ts"]
    monitor["exit_reason"] = ""
    monitor.loc[monitor["is_exit"], "exit_reason"] = reason

    ce_exit, pe_exit = float(exit_row["ce_close"]), float(exit_row["pe_close"])
    quantity = resolve_lot_quantity(cfg, ce_inst.get("lot_size") or pe_inst.get("lot_size") or 65)
    gross_pnl = (entry_value - ce_exit - pe_exit) * quantity
    charges = total_trade_cost(ce_entry_px, pe_entry_px, ce_exit, pe_exit, quantity, cfg.costs)
    trade = CalibratedTrade(
        dte = f"{(expiry - day).days}DT",
        date=day, 
        expiry=expiry, 
        strike=strike, 
        entry_time=entry_dt, 
        exit_time=exit_row["ts"],
        spot_entry=float(entry_spot_row["close"]), 
        spot_exit=float(exit_row["spot_close"]),
        ce_entry=ce_entry_px, 
        pe_entry=pe_entry_px, 
        ce_exit=ce_exit, 
        pe_exit=pe_exit,
        entry_straddle_value=entry_value, 
        exit_straddle_value=ce_exit + pe_exit,
        target_pct=target_pct, 
        stop_loss_pct=stop_loss_pct,
        target_straddle_value=target_value, 
        stop_loss_straddle_value=stop_value,
        exit_reason=reason, 
        quantity=quantity, 
        gross_pnl=gross_pnl, 
        charges=charges,
        net_pnl=gross_pnl - charges,
    )
    return trade, monitor


def trade_frame(trades: list[CalibratedTrade]) -> pd.DataFrame:
    return pd.DataFrame(asdict(trade) for trade in trades)
