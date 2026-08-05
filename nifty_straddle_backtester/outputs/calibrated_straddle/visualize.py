"""Plot daily net P&L for expiry-day (0DTE) calibrated straddles."""

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


OUTPUT_DIR = Path(__file__).resolve().parent
TRADE_LOG = OUTPUT_DIR / "trade_log.csv"
PLOT_FILE = OUTPUT_DIR / "1dte_daily_pnl.png"


def plot_1dte_daily_pnl(trade_log: pd.DataFrame) -> None:
    """Save one bar per trading date, using only 1DTE trades."""
    required_columns = {"dte", "date", "net_pnl"}
    missing_columns = required_columns - set(trade_log.columns)
    if missing_columns:
        raise ValueError(f"trade_log.csv is missing columns: {sorted(missing_columns)}")

    one_dte = trade_log.loc[
        trade_log["dte"].astype(str).str.strip().str.upper().eq("1DT")
    ].copy()
    if one_dte.empty:
        raise ValueError("No 1DTE trades found in trade_log.csv.")

    one_dte["date"] = pd.to_datetime(one_dte["date"])
    daily_pnl = one_dte.groupby("date", as_index=True)["net_pnl"].sum().sort_index()

    fig, ax = plt.subplots(figsize=(max(12, len(daily_pnl) * 0.22), 6))
    colors = ["#16a34a" if pnl >= 0 else "#dc2626" for pnl in daily_pnl]
    ax.bar(daily_pnl.index, daily_pnl, width=0.8, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title("1DTE Daily Net P&L")
    ax.set_xlabel("Trade date")
    ax.set_ylabel("Net P&L")
    ax.grid(axis="y", alpha=0.25)
    fig.autofmt_xdate(rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(PLOT_FILE, dpi=150, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    plot_1dte_daily_pnl(pd.read_csv(TRADE_LOG))
    print(f"Saved 1DTE daily P&L chart to {PLOT_FILE}")
