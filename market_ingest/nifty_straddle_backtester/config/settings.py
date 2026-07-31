"""
Central configuration for the NIFTY ATM Short Straddle backtesting framework.

Everything a user might want to tune (SL sweep range, session times, costs,
position sizing) lives here as dataclasses so the rest of the codebase never
hardcodes a "magic number".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import List


@dataclass(frozen=True)
class SessionConfig:
    """Intraday session timing."""
    entry_time: time = time(9, 30)
    exit_time: time = time(15, 15)
    candle_interval_minutes: int = 1


@dataclass(frozen=True)
class StopLossSweepConfig:
    """Defines the SL% grid to optimize over (10% -> 200%, step 1% by default)."""
    start_pct: float = 10.0
    end_pct: float = 200.0
    step_pct: float = 10.0

    def grid(self) -> List[float]:
        n_steps = int(round((self.end_pct - self.start_pct) / self.step_pct)) + 1
        return [round(self.start_pct + i * self.step_pct, 4) for i in range(n_steps)]


@dataclass(frozen=True)
class CostConfig:
    """
    Per-lot / per-order cost model. All costs can be individually toggled off
    for "gross" runs. Values are illustrative NSE-ish defaults in INR /
    percentage terms - override for your own broker.
    """
    enabled: bool = True

    brokerage_per_order: float = 20.0          # flat per executed order (buy or sell)
    stt_sell_pct: float = 0.0625               # % of premium, on SELL side (options STT)
    exchange_txn_pct: float = 0.05             # % of premium, both sides
    gst_pct: float = 18.0                      # % applied on (brokerage + exchange charges)
    sebi_charges_pct: float = 0.0001           # % of turnover
    stamp_duty_buy_pct: float = 0.003          # % of premium, BUY side only
    slippage_pct: float = 0.5                  # % of premium, applied on both entry & exit

    def multiplier(self) -> float:
        return 1.0 if self.enabled else 0.0


@dataclass(frozen=True)
class PositionSizingConfig:
    """
    Position sizing. `mode` selects the active sizing method; the engine reads
    only the field relevant to the selected mode. Extra modes can be added
    later (percentage_capital, risk_based) without touching engine code.
    """
    mode: str = "lots"                 # "lots" | "fixed_capital" | "percentage_capital" | "risk_based"
    lots: int = 1
    lot_size_override: int | None = None   # if None, pulled from `underlying`/`instrument` table
    fixed_capital: float = 0.0
    percentage_capital: float = 0.0
    risk_per_trade_pct: float = 0.0


@dataclass(frozen=True)
class BacktestConfig:
    """Top-level config bundling everything a single backtest run needs."""
    underlying_symbol: str = "NIFTY"
    exchange: str = "NSE"

    start_date: str = "2024-01-01"     # inclusive, ISO format
    end_date: str = "2024-12-31"       # inclusive, ISO format

    session: SessionConfig = field(default_factory=SessionConfig)
    sl_sweep: StopLossSweepConfig = field(default_factory=StopLossSweepConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    sizing: PositionSizingConfig = field(default_factory=PositionSizingConfig)

    position_manager_name: str = "square_off_on_trigger"   # pluggable strategy, see position_manager.py

    output_dir: str = "outputs"
