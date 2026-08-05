"""
Strategy 1 - NIFTY ATM Short Straddle with Spot-Based Dynamic Trend Management.

This module implements only the "what happens on one trading day, for one
SL%" simulation. Multi-day looping and multi-SL% sweeping live in
`engine/backtester.py`; this keeps entry logic, trigger math, and exit
bookkeeping independent of the optimisation loop around them.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
ist = ZoneInfo("Asia/Kolkata")

from config.settings import BacktestConfig
from db.repository import MarketDataRepository
from engine.costs import total_trade_cost
from strategy.position_manager import (
    Action, Decision, LegState, PositionContext, PositionManager,
)


def resolve_atm_strike(spot: float, strike_step: float) -> float:
    """Nearest strike to spot, per the step size configured on `underlying`."""
    if strike_step is None or strike_step <= 0:
        raise ValueError("strike_step must be a positive number from the `underlying` table")
    return round(round(spot / strike_step) * strike_step, 2)


def resolve_lot_quantity(cfg: BacktestConfig, instrument_lot_size: int = 65) -> int:
    sizing = cfg.sizing
    lot_size = int(instrument_lot_size) if instrument_lot_size else 65
    if sizing.mode == "lots":
        return sizing.lots * lot_size
    if sizing.mode == "fixed_capital":
        # placeholder: needs a reference premium/margin figure to size against
        raise NotImplementedError("fixed_capital sizing needs margin/premium reference data.")
    if sizing.mode == "percentage_capital":
        raise NotImplementedError("percentage_capital sizing needs account equity input.")
    if sizing.mode == "risk_based":
        raise NotImplementedError("risk_based sizing needs a stop-distance-to-risk mapping.")
    raise ValueError(f"Unknown sizing mode {sizing.mode!r}")


@dataclass
class TradeResult:
    date: dt.date
    entry_time: dt.datetime
    exit_time: dt.datetime
    strike: float
    ce_entry: float
    pe_entry: float
    ce_exit: float
    pe_exit: float
    spot_entry: float
    spot_exit: float
    combined_premium: float
    stop_loss_pct: float
    exit_reason: str
    quantity: int
    gross_pnl: float
    charges: float
    net_pnl: float


def _nearest_candle(df: pd.DataFrame, target: dt.datetime) -> Optional[pd.Series]:
    if df.empty:
        return None
    idx = (df["ts"] - target).abs().idxmin()
    return df.loc[idx]


def simulate_day(
    day: dt.date,
    stop_loss_pct: float,
    cfg: BacktestConfig,
    repo: MarketDataRepository,
    underlying_id: int,
    strike_step: float,
    spot_instrument_id: int,
    position_manager: PositionManager,
) -> Optional[TradeResult]:
    """Run the full entry -> monitor -> exit cycle for a single trading day."""

    spot_df = repo.get_spot_ohlcv(spot_instrument_id, str(day), str(day))
    # print(spot_df["ts"])
    if spot_df.empty:
        return None

    entry_dt = dt.datetime.combine(day, cfg.session.entry_time)
    # print(entry_dt)
    exit_dt = dt.datetime.combine(day, cfg.session.exit_time)
    # print(exit_dt)

    session_df = spot_df[(spot_df["ts"] >= entry_dt) & (spot_df["ts"] <= exit_dt)].reset_index(drop=True)
    # print("Yaha aana hai")
    if session_df.empty:
        return None

    # --- Step 1 & 2: entry spot + ATM strike -------------------------------
    entry_candle = _nearest_candle(session_df, entry_dt)
    spot_entry = float(entry_candle["close"])
    strike = resolve_atm_strike(spot_entry, strike_step)

    # find the expiry active "today" (nearest weekly/monthly expiry >= day)
    expiry = repo.find_nearest_complete_expiry(
        underlying_id, str(day), str(day + dt.timedelta(days=45)),
    )
    if expiry is None:
        return None


    ce_inst = repo.get_option_instrument(underlying_id, expiry, strike, "CE")
    pe_inst = repo.get_option_instrument(underlying_id, expiry, strike, "PE")
    if ce_inst is None or pe_inst is None:
        return None

    ce_df = repo.get_option_ohlcv(ce_inst["instrument_id"], day)
    pe_df = repo.get_option_ohlcv(pe_inst["instrument_id"], day)
    if ce_df.empty or pe_df.empty:
        return None

    ce_entry_candle = _nearest_candle(ce_df[ce_df["ts"] <= entry_dt + dt.timedelta(minutes=1)], entry_dt)
    pe_entry_candle = _nearest_candle(pe_df[pe_df["ts"] <= entry_dt + dt.timedelta(minutes=1)], entry_dt)
    if ce_entry_candle is None or pe_entry_candle is None:
        return None

    ce_entry_px = float(ce_entry_candle["close"])
    print(f"CE Entry Price: {ce_entry_px}")
    pe_entry_px = float(pe_entry_candle["close"])
    print(f"PE Entry Price: {pe_entry_px}")
    combined_premium = ce_entry_px + pe_entry_px


    # --- Step: SL distance & spot trigger levels ---------------------------
    distance = combined_premium * (stop_loss_pct / 100.0)
    upper_trigger = spot_entry + distance
    lower_trigger = spot_entry - distance

    quantity = resolve_lot_quantity(cfg, ce_inst["lot_size"])

    ce_state, pe_state = LegState.OPEN, LegState.OPEN

    ce_exit_px, pe_exit_px = None, None
    exit_reason, exit_ts, spot_exit = None, None, None


    monitor_df = session_df[session_df["ts"] >= entry_dt].reset_index(drop=True)

    for i, row in monitor_df.iterrows():
        is_last = i == len(monitor_df) - 1
        ctx = PositionContext(
            ts=row["ts"], spot=float(row["close"]),
            upper_trigger=upper_trigger, lower_trigger=lower_trigger,
            ce_state=ce_state, pe_state=pe_state,
            ce_ltp=ce_entry_px, pe_ltp=pe_entry_px,
            entry_spot=spot_entry, combined_premium_entry=combined_premium,
            is_last_candle_of_day=is_last,
        )
        decision: Decision = position_manager.decide(ctx)

        if decision.action == Action.HOLD:
            continue

        if decision.action == Action.EXIT_BOTH:
            ce_candle = _nearest_candle(ce_df[ce_df["ts"] <= row["ts"]], row["ts"])
            pe_candle = _nearest_candle(pe_df[pe_df["ts"] <= row["ts"]], row["ts"])
            ce_exit_px = float(ce_candle["close"]) if ce_candle is not None else ce_entry_px
            pe_exit_px = float(pe_candle["close"]) if pe_candle is not None else pe_entry_px
            ce_state = pe_state = LegState.CLOSED
            exit_reason, exit_ts, spot_exit = decision.reason, row["ts"], float(row["close"])
            break

        # Other actions (EXIT_CE, EXIT_PE, TRAIL_STOP, SHIFT_STRIKE, REENTER,
        # HEDGE) are wired via the position_manager registry; the branching
        # here can be extended as those managers move from placeholder to
        # real implementations.

    # print("5")
    if exit_ts is None:
        # No manager decision closed it (shouldn't happen with the default
        # manager, which always force-exits on the last candle) - fail safe.
        last_row = monitor_df.iloc[-1]
        ce_candle = _nearest_candle(ce_df[ce_df["ts"] <= last_row["ts"]], last_row["ts"])
        pe_candle = _nearest_candle(pe_df[pe_df["ts"] <= last_row["ts"]], last_row["ts"])
        ce_exit_px = float(ce_candle["close"]) if ce_candle is not None else ce_entry_px
        pe_exit_px = float(pe_candle["close"]) if pe_candle is not None else pe_entry_px
        exit_reason, exit_ts, spot_exit = "session_exit_15_15", last_row["ts"], float(last_row["close"])

    gross_pnl = (ce_entry_px - ce_exit_px + pe_entry_px - pe_exit_px) * quantity
    charges = total_trade_cost(ce_entry_px, pe_entry_px, ce_exit_px, pe_exit_px, quantity, cfg.costs)
    net_pnl = gross_pnl - charges

    # print("6")

    return TradeResult(
        date=day, entry_time=entry_dt, exit_time=exit_ts,
        strike=strike, ce_entry=ce_entry_px, pe_entry=pe_entry_px,
        ce_exit=ce_exit_px, pe_exit=pe_exit_px,
        spot_entry=spot_entry, spot_exit=spot_exit,
        combined_premium=combined_premium, stop_loss_pct=stop_loss_pct,
        exit_reason=exit_reason, quantity=quantity,
        gross_pnl=gross_pnl, charges=charges, net_pnl=net_pnl,
    )
