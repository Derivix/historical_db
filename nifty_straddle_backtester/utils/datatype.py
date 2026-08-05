from dataclasses import asdict, dataclass
import datetime as dt
import pandas as pd
from config.settings import CostConfig
from engine.costs import compute_leg_cost

@dataclass(frozen=True)
class SDBreakoutConfig:
    entry_time: dt.time = dt.time(10, 2)
    exit_time: dt.time = dt.time(15, 15)
    initial_premium: float = 35.0
    trigger_straddle_premium: float = 30.0
    adjustment_premium: float = 180.0
    replacement_target_pct: float = 60.0
    replacement_stop_pct: float = 40.0
    portfolio_loss_limit: float = 4_000.0
    strike_search_steps: int = 50
    lots: int = 1
    allowed_dte: tuple[int, ...] | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    costs: CostConfig = CostConfig()

@dataclass(frozen=True)
class SDBreakout935:
    entry_time: dt.time = dt.time(10, 5)
    exit_time: dt.time = dt.time(15, 15)
    initial_premium: float = 50.0
    #ATM movement value -2,-1,0,1,2 
    atm_offset: int = 0
    adjustment_premium: float = 60.0
    # After the reversal signal, wait for this additional move in the
    # reversal-monitor straddle before placing the adjustment short.
    wait_and_trade_pct: float = -15.0
    reverse_trend_pct: float = 5.0
    # The newly sold adjustment option is bought back if its premium rises by
    # this percentage.  The remaining original short stays open until the
    # combined-loss limit or the time exit.
    adjustment_stop_pct: float = 25.0
    portfolio_loss_limit: float = 3_000.0
    strike_search_steps: int = 50
    lots: int = 1
    allowed_dte: tuple[int, ...] | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    costs: CostConfig = CostConfig()


@dataclass
class _Leg:
    option_type: str
    instrument_id: int
    ticker: str
    strike: float
    entry_time: dt.datetime
    entry_price: float
    quantity: int
    prices: pd.DataFrame
    exit_time: dt.datetime | None = None
    exit_price: float | None = None

    def close(self, when: dt.datetime, price: float) -> None:
        self.exit_time, self.exit_price = when, float(price)

    @property
    def gross_pnl(self) -> float:
        return 0.0 if self.exit_price is None else (self.entry_price - self.exit_price) * self.quantity
