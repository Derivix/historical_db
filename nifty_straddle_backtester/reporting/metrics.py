"""
Performance metric computations, shared by both the per-SL% parameter
summary and any single-run report.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(equity_curve: pd.Series) -> float:
    running_max = equity_curve.cummax()
    drawdown = equity_curve - running_max
    return float(drawdown.min()) if len(drawdown) else 0.0


def drawdown_series(equity_curve: pd.Series) -> pd.Series:
    running_max = equity_curve.cummax()
    return equity_curve - running_max


def max_streak(is_win: pd.Series, want_win: bool) -> int:
    streak = best = 0
    for w in is_win:
        if w == want_win:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def sharpe_ratio(daily_returns: pd.Series, periods_per_year: int = 252, risk_free: float = 0.0) -> float:
    if daily_returns.std(ddof=1) == 0 or len(daily_returns) < 2:
        return 0.0
    excess = daily_returns - risk_free / periods_per_year
    return float(np.sqrt(periods_per_year) * excess.mean() / daily_returns.std(ddof=1))


def sortino_ratio(daily_returns: pd.Series, periods_per_year: int = 252, risk_free: float = 0.0) -> float:
    downside = daily_returns[daily_returns < 0]
    if downside.std(ddof=1) == 0 or len(downside) < 2:
        return 0.0
    excess = daily_returns - risk_free / periods_per_year
    return float(np.sqrt(periods_per_year) * excess.mean() / downside.std(ddof=1))


def calmar_ratio(cagr: float, max_dd: float) -> float:
    if max_dd == 0:
        return 0.0
    return float(cagr / abs(max_dd))


def cagr(equity_curve: pd.Series, trading_days_per_year: int = 252) -> float:
    if len(equity_curve) < 2 or equity_curve.iloc[0] <= 0:
        return 0.0
    n_days = len(equity_curve)
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0]
    years = n_days / trading_days_per_year
    if years <= 0 or total_return <= 0:
        return 0.0
    return float(total_return ** (1 / years) - 1)


def summarize_trades(trade_log: pd.DataFrame, starting_capital: float = 1_000_000.0) -> dict:
    """Compute the full parameter-summary metric set for one SL% bucket's trade log."""
    if trade_log.empty:
        return {
            "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "profit_factor": 0.0, "win_rate": 0.0, "avg_winner": 0.0, "avg_loser": 0.0,
            "expectancy": 0.0, "total_trades": 0, "max_drawdown": 0.0, "cagr": 0.0,
            "sharpe": 0.0, "sortino": 0.0, "calmar": 0.0, "avg_daily_return": 0.0,
            "max_win_streak": 0, "max_loss_streak": 0,
        }

    pnl = trade_log["net_pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]

    gross_profit = float(wins.sum())
    gross_loss = float(losses.sum())
    net_profit = float(pnl.sum())
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else float("inf") if gross_profit > 0 else 0.0
    win_rate = float(len(wins) / len(pnl)) if len(pnl) else 0.0
    avg_winner = float(wins.mean()) if len(wins) else 0.0
    avg_loser = float(losses.mean()) if len(losses) else 0.0
    expectancy = float(pnl.mean()) if len(pnl) else 0.0

    equity = starting_capital + pnl.cumsum()
    daily_returns = equity.pct_change().dropna()

    mdd = max_drawdown(equity)
    cg = cagr(equity)
    is_win = pnl > 0

    return {
        "net_profit": net_profit,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "avg_winner": avg_winner,
        "avg_loser": avg_loser,
        "expectancy": expectancy,
        "total_trades": int(len(pnl)),
        "max_drawdown": mdd,
        "cagr": cg,
        "sharpe": sharpe_ratio(daily_returns),
        "sortino": sortino_ratio(daily_returns),
        "calmar": calmar_ratio(cg, mdd),
        "avg_daily_return": float(daily_returns.mean()) if len(daily_returns) else 0.0,
        "max_win_streak": max_streak(is_win, True),
        "max_loss_streak": max_streak(is_win, False),
    }
