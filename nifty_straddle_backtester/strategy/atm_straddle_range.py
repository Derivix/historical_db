"""Daily range of a NIFTY ATM straddle selected from the entry-time spot."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.settings import BacktestConfig
from db.repository import MarketDataRepository
from strategy.straddle import _nearest_candle, resolve_atm_strike


@dataclass(frozen=True)
class DailyStraddleRange:
    """The intraday premium range for one fixed ATM CE + PE pair."""

    date: dt.date
    expiry: dt.date
    strike: float
    entry_time: dt.datetime
    entry_spot: float
    ce_entry: float
    pe_entry: float
    entry_straddle_value: float
    exit_time: dt.datetime
    ce_exit: float
    pe_exit: float
    exit_straddle_value: float
    min_straddle_value: float
    min_time: dt.datetime
    max_straddle_value: float
    max_time: dt.datetime
    observations: int


def _session_option_prices(
    spot_session: pd.DataFrame, ce_df: pd.DataFrame, pe_df: pd.DataFrame,
) -> pd.DataFrame:
    """Align option closes to each spot timestamp using the latest known tick.

    ``merge_asof`` keeps the monitoring series usable when an option has an
    occasional missing minute while never looking ahead to a future price.
    """
    monitor = spot_session[["ts"]].sort_values("ts").copy()
    ce = ce_df[["ts", "close"]].rename(columns={"close": "ce_close"}).sort_values("ts")
    pe = pe_df[["ts", "close"]].rename(columns={"close": "pe_close"}).sort_values("ts")
    monitor = pd.merge_asof(monitor, ce, on="ts", direction="backward")
    monitor = pd.merge_asof(monitor, pe, on="ts", direction="backward")
    monitor = monitor.dropna(subset=["ce_close", "pe_close"])
    monitor["straddle_value"] = monitor["ce_close"].astype(float) + monitor["pe_close"].astype(float)
    return monitor


def measure_daily_atm_straddle_range(
    day: dt.date,
    cfg: BacktestConfig,
    repo: MarketDataRepository,
    underlying_id: int,
    strike_step: float,
    spot_instrument_id: int,
) -> Optional[DailyStraddleRange]:
    """Pick the entry-time ATM straddle and return its intraday min/max value."""
    spot_df = repo.get_spot_ohlcv(spot_instrument_id, str(day), str(day))
    if spot_df.empty:
        return None

    entry_time = dt.datetime.combine(day, cfg.session.entry_time)
    exit_time = dt.datetime.combine(day, cfg.session.exit_time)
    session = spot_df[(spot_df["ts"] >= entry_time) & (spot_df["ts"] <= exit_time)].copy()
    if session.empty:
        return None

    entry_spot_candle = _nearest_candle(session, entry_time)
    if entry_spot_candle is None:
        return None
    entry_spot = float(entry_spot_candle["close"])
    strike = resolve_atm_strike(entry_spot, strike_step)

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

    monitored = _session_option_prices(session, ce_df, pe_df)
    if monitored.empty:
        return None

    entry_ce = _nearest_candle(ce_df[ce_df["ts"] <= entry_time], entry_time)
    entry_pe = _nearest_candle(pe_df[pe_df["ts"] <= entry_time], entry_time)
    if entry_ce is None or entry_pe is None:
        return None

    exit_ce = _nearest_candle(ce_df[ce_df["ts"] <= exit_time], exit_time)
    exit_pe = _nearest_candle(pe_df[pe_df["ts"] <= exit_time], exit_time)
    if exit_ce is None or exit_pe is None:
        return None

    min_row = monitored.loc[monitored["straddle_value"].idxmin()]
    max_row = monitored.loc[monitored["straddle_value"].idxmax()]
    ce_entry = float(entry_ce["close"])
    pe_entry = float(entry_pe["close"])
    return DailyStraddleRange(
        date=day, expiry=expiry, strike=strike, entry_time=entry_time,
        entry_spot=entry_spot, ce_entry=ce_entry, pe_entry=pe_entry,
        entry_straddle_value=ce_entry + pe_entry,
        exit_time=exit_time, ce_exit=float(exit_ce["close"]), pe_exit=float(exit_pe["close"]),
        exit_straddle_value=float(exit_ce["close"]) + float(exit_pe["close"]),
        min_straddle_value=float(min_row["straddle_value"]), min_time=min_row["ts"],
        max_straddle_value=float(max_row["straddle_value"]), max_time=max_row["ts"],
        observations=len(monitored),
    )
