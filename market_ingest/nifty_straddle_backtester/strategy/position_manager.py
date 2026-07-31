"""
Position management is deliberately isolated from the rest of the engine.

The backtester calls `PositionManager.decide(ctx)` on every candle. The
manager looks at the current market state + current leg status and returns
a `Decision`. It never touches PnL, cost, or data-fetching code - that keeps
new management styles (trailing SL, strike-shift, re-entry, hedging, ...)
pluggable without touching `engine/backtester.py`.

To add a new management style:
    1. Subclass PositionManager
    2. Implement decide()
    3. Register it in POSITION_MANAGER_REGISTRY at the bottom of this file
    4. Set BacktestConfig.position_manager_name to your registered key
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class LegState(Enum):
    OPEN = auto()
    CLOSED = auto()


class Action(Enum):
    HOLD = auto()               # do nothing this candle
    EXIT_CE = auto()             # close CE leg only
    EXIT_PE = auto()             # close PE leg only
    EXIT_BOTH = auto()           # close both legs (full stop-out)
    HOLD_ONE_LEG = auto()        # explicit no-op on the untouched leg after other leg exits
    TRAIL_STOP = auto()          # recompute trigger levels tighter
    SHIFT_STRIKE = auto()        # roll to a new ATM strike
    REENTER = auto()             # open a fresh straddle/leg after an exit
    HEDGE = auto()                # add a protective long option


@dataclass
class PositionContext:
    """Everything a management rule might need to make a decision."""
    ts: object                      # current candle timestamp
    spot: float
    upper_trigger: float
    lower_trigger: float
    ce_state: LegState
    pe_state: LegState
    ce_ltp: float
    pe_ltp: float
    entry_spot: float
    combined_premium_entry: float
    is_last_candle_of_day: bool


@dataclass
class Decision:
    action: Action
    reason: str
    new_upper_trigger: Optional[float] = None
    new_lower_trigger: Optional[float] = None


class PositionManager:
    """Abstract base class. All management styles implement `decide`."""

    name: str = "base"

    def decide(self, ctx: PositionContext) -> Decision:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Default, fully-implemented style: exit both legs the instant either the
# upper or lower spot trigger is touched. This is "Strategy 1" as described
# in the spec - the SL% sweep evaluates exactly this behaviour.
# ---------------------------------------------------------------------------
class SquareOffOnTrigger(PositionManager):
    name = "square_off_on_trigger"

    def decide(self, ctx: PositionContext) -> Decision:
        if ctx.ce_state == LegState.CLOSED and ctx.pe_state == LegState.CLOSED:
            return Decision(Action.HOLD, reason="already flat")

        triggered = ctx.spot >= ctx.upper_trigger or ctx.spot <= ctx.lower_trigger
        if triggered:
            reason = "upper_trigger_hit" if ctx.spot >= ctx.upper_trigger else "lower_trigger_hit"
            return Decision(Action.EXIT_BOTH, reason=reason)

        if ctx.is_last_candle_of_day:
            return Decision(Action.EXIT_BOTH, reason="session_exit_15_15")

        return Decision(Action.HOLD, reason="within_range")


# ---------------------------------------------------------------------------
# Placeholders for future management styles - skeletons only, per the spec's
# request for pluggable: exit-one-leg, hold-one-leg, trail, shift-strike,
# re-enter, hedge. Wire these up later without touching the engine.
# ---------------------------------------------------------------------------
class ExitTriggeredLegOnly(PositionManager):
    """On trigger, exit only the leg on the losing side; hold the other leg open."""
    name = "exit_triggered_leg_only"

    def decide(self, ctx: PositionContext) -> Decision:
        # TODO: implement - e.g. upper trigger -> CE is losing -> EXIT_CE, HOLD_ONE_LEG on PE
        raise NotImplementedError("Plug in your exit-one-leg management rule here.")


class TrailStopManager(PositionManager):
    """Ratchet the SL distance tighter as the position moves favourably."""
    name = "trail_stop"

    def decide(self, ctx: PositionContext) -> Decision:
        # TODO: implement trailing logic, returning Action.TRAIL_STOP with
        # new_upper_trigger / new_lower_trigger populated.
        raise NotImplementedError("Plug in your trailing-stop management rule here.")


class ShiftStrikeManager(PositionManager):
    """Roll the untouched or both legs to a new ATM strike after a trigger."""
    name = "shift_strike"

    def decide(self, ctx: PositionContext) -> Decision:
        raise NotImplementedError("Plug in your strike-shift management rule here.")


class ReEntryManager(PositionManager):
    """Re-enter a fresh straddle/leg after being stopped out, subject to rules."""
    name = "re_entry"

    def decide(self, ctx: PositionContext) -> Decision:
        raise NotImplementedError("Plug in your re-entry management rule here.")


class HedgeManager(PositionManager):
    """Buy a protective OTM option once a trigger fires instead of exiting."""
    name = "hedge"

    def decide(self, ctx: PositionContext) -> Decision:
        raise NotImplementedError("Plug in your hedging management rule here.")


POSITION_MANAGER_REGISTRY: dict[str, type[PositionManager]] = {
    cls.name: cls
    for cls in [
        SquareOffOnTrigger,
        ExitTriggeredLegOnly,
        TrailStopManager,
        ShiftStrikeManager,
        ReEntryManager,
        HedgeManager,
    ]
}


def get_position_manager(name: str) -> PositionManager:
    try:
        return POSITION_MANAGER_REGISTRY[name]()
    except KeyError as e:
        raise ValueError(
            f"Unknown position manager {name!r}. Available: {list(POSITION_MANAGER_REGISTRY)}"
        ) from e
