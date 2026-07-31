"""
Visualisation suite. Every function takes a trade log / daily summary
DataFrame and saves a PNG into `output_dir`. Kept separate from metrics so
you can swap matplotlib for plotly without touching calculation code.
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from reporting.metrics import drawdown_series


def _save(fig, output_dir: str, name: str):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{name}.png")
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_equity_curve(trade_log: pd.DataFrame, output_dir: str, starting_capital: float = 1_000_000.0):
    equity = starting_capital + trade_log["Net PnL"].cumsum()
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(trade_log["Date"], equity, color="#2563eb")
    ax.set_title("Equity Curve")
    ax.set_xlabel("Date"); ax.set_ylabel("Equity")
    return _save(fig, output_dir, "equity_curve")


def plot_drawdown_curve(trade_log: pd.DataFrame, output_dir: str, starting_capital: float = 1_000_000.0):
    equity = starting_capital + trade_log["Net PnL"].cumsum()
    dd = drawdown_series(equity)
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.fill_between(trade_log["Date"], dd, 0, color="#dc2626", alpha=0.5)
    ax.set_title("Drawdown Curve")
    return _save(fig, output_dir, "drawdown_curve")


def plot_monthly_heatmap(trade_log: pd.DataFrame, output_dir: str):
    df = trade_log.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df["Year"] = df["Date"].dt.year
    df["Month"] = df["Date"].dt.month
    pivot = df.groupby(["Year", "Month"])["Net PnL"].sum().unstack("Month")
    fig, ax = plt.subplots(figsize=(10, max(2, 0.6 * len(pivot))))
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(pivot.shape[1])); ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(pivot.shape[0])); ax.set_yticklabels(pivot.index)
    ax.set_title("Monthly Returns Heatmap (Net PnL)")
    fig.colorbar(im, ax=ax)
    return _save(fig, output_dir, "monthly_returns_heatmap")


def plot_daily_pnl(trade_log: pd.DataFrame, output_dir: str):
    daily = trade_log.groupby("Date")["Net PnL"].sum()
    fig, ax = plt.subplots(figsize=(10, 4))
    colors = ["#16a34a" if v >= 0 else "#dc2626" for v in daily.values]
    ax.bar(daily.index.astype(str), daily.values, color=colors)
    ax.set_title("Daily PnL")
    ax.tick_params(axis="x", rotation=90)
    return _save(fig, output_dir, "daily_pnl")


def plot_returns_histogram(trade_log: pd.DataFrame, output_dir: str):
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(trade_log["Net PnL"], bins=30, color="#2563eb", alpha=0.8)
    ax.set_title("Histogram of Trade Returns")
    return _save(fig, output_dir, "returns_histogram")


def plot_trade_distribution(trade_log: pd.DataFrame, output_dir: str):
    fig, ax = plt.subplots(figsize=(8, 4))
    trade_log["Exit Reason"].value_counts().plot(kind="bar", ax=ax, color="#7c3aed")
    ax.set_title("Trade Distribution by Exit Reason")
    return _save(fig, output_dir, "trade_distribution")


def plot_rolling_drawdown(trade_log: pd.DataFrame, output_dir: str, starting_capital: float = 1_000_000.0, window: int = 20):
    equity = starting_capital + trade_log["Net PnL"].cumsum()
    rolling_max = equity.rolling(window, min_periods=1).max()
    rolling_dd = equity - rolling_max
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(trade_log["Date"], rolling_dd, color="#ea580c")
    ax.set_title(f"Rolling Drawdown ({window}-trade window)")
    return _save(fig, output_dir, "rolling_drawdown")


def plot_cumulative_returns(trade_log: pd.DataFrame, output_dir: str, starting_capital: float = 1_000_000.0):
    equity = starting_capital + trade_log["Net PnL"].cumsum()
    cum_return_pct = (equity / starting_capital - 1) * 100
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(trade_log["Date"], cum_return_pct, color="#059669")
    ax.set_title("Cumulative Returns (%)")
    return _save(fig, output_dir, "cumulative_returns")


def plot_win_loss_distribution(trade_log: pd.DataFrame, output_dir: str):
    wins = int((trade_log["Net PnL"] > 0).sum())
    losses = int((trade_log["Net PnL"] <= 0).sum())
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.pie([wins, losses], labels=["Wins", "Losses"], autopct="%1.1f%%",
           colors=["#16a34a", "#dc2626"])
    ax.set_title("Win / Loss Distribution")
    return _save(fig, output_dir, "win_loss_distribution")


def generate_all_plots(trade_log: pd.DataFrame, output_dir: str, starting_capital: float = 1_000_000.0) -> list[str]:
    if trade_log.empty:
        return []
    trade_log = trade_log.sort_values("Date").reset_index(drop=True)
    plot_dir = os.path.join(output_dir, "plots")
    funcs = [
        plot_equity_curve, plot_drawdown_curve, plot_monthly_heatmap,
        plot_daily_pnl, plot_returns_histogram, plot_trade_distribution,
        plot_rolling_drawdown, plot_cumulative_returns, plot_win_loss_distribution,
    ]
    paths = []
    for f in funcs:
        try:
            if "starting_capital" in f.__code__.co_varnames:
                paths.append(f(trade_log, plot_dir, starting_capital))
            else:
                paths.append(f(trade_log, plot_dir))
        except Exception as e:
            print(f"[plots] skipped {f.__name__}: {e}")
    return paths
