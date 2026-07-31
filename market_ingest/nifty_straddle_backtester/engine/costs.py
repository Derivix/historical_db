"""
Transaction cost model.

Each leg (CE sell, PE sell, CE buy-to-close, PE buy-to-close) is charged
independently, then summed. All costs are individually configurable and can
be switched off in one place (`CostConfig.enabled`) for gross-PnL runs.
"""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import CostConfig


@dataclass
class LegCost:
    brokerage: float
    stt: float
    exchange_txn: float
    gst: float
    sebi: float
    stamp_duty: float
    slippage: float

    @property
    def total(self) -> float:
        return (
            self.brokerage + self.stt + self.exchange_txn + self.gst
            + self.sebi + self.stamp_duty + self.slippage
        )


def compute_leg_cost(
    premium: float,
    quantity: int,
    side: str,          # "BUY" or "SELL"
    cfg: CostConfig,
) -> LegCost:
    """
    premium: option premium per unit (not multiplied by lot size)
    quantity: total quantity traded (lots * lot_size)
    """
    m = cfg.multiplier()
    turnover = premium * quantity

    brokerage = cfg.brokerage_per_order * m
    stt = (cfg.stt_sell_pct / 100.0) * turnover * m if side == "SELL" else 0.0
    exchange_txn = (cfg.exchange_txn_pct / 100.0) * turnover * m
    stamp_duty = (cfg.stamp_duty_buy_pct / 100.0) * turnover * m if side == "BUY" else 0.0
    sebi = (cfg.sebi_charges_pct / 100.0) * turnover * m
    gst = (cfg.gst_pct / 100.0) * (brokerage + exchange_txn) * m
    slippage = (cfg.slippage_pct / 100.0) * turnover * m

    return LegCost(
        brokerage=brokerage, stt=stt, exchange_txn=exchange_txn,
        gst=gst, sebi=sebi, stamp_duty=stamp_duty, slippage=slippage,
    )


def total_trade_cost(
    ce_entry: float, pe_entry: float, ce_exit: float, pe_exit: float,
    quantity: int, cfg: CostConfig,
) -> float:
    """Full round-trip cost for a short straddle: sell CE+PE to open, buy CE+PE to close."""
    legs = [
        compute_leg_cost(ce_entry, quantity, "SELL", cfg),
        compute_leg_cost(pe_entry, quantity, "SELL", cfg),
        compute_leg_cost(ce_exit, quantity, "BUY", cfg),
        compute_leg_cost(pe_exit, quantity, "BUY", cfg),
    ]
    return sum(leg.total for leg in legs)
