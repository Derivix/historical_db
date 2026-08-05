"""
Turns raw TradeResult lists into the three deliverables the spec asks for:
    1. Trade log            (one row per trade)
    2. Daily summary         (one row per trading day, per SL%)
    3. Parameter summary      (one row per SL%, across the whole sweep)
"""
from __future__ import annotations

import os
from typing import Iterable

import pandas as pd

from reporting.metrics import summarize_trades, max_drawdown


def build_trade_log(trades: Iterable) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({
            "Date": t.date, "Entry Time": t.entry_time, "Exit Time": t.exit_time,
            "Strike": t.strike, "CE Entry": t.ce_entry, "PE Entry": t.pe_entry,
            "CE Exit": t.ce_exit, "PE Exit": t.pe_exit,
            "Spot Entry": t.spot_entry, "Spot Exit": t.spot_exit,
            "Combined Premium": t.combined_premium, "Stop Loss %": t.stop_loss_pct,
            "Exit Reason": t.exit_reason, "Gross PnL": t.gross_pnl,
            "Charges": t.charges, "Net PnL": t.net_pnl,
        })
    return pd.DataFrame(rows)


def build_daily_summary(trade_log: pd.DataFrame, stop_loss_pct: float) -> pd.DataFrame:
    if trade_log.empty:
        return pd.DataFrame()

    rows = []
    for date, day_df in trade_log.groupby("Date"):
        pnl = day_df["Net PnL"]
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        equity = pnl.cumsum()
        rows.append({
            "Date": date,
            "Entry Spot": day_df["Spot Entry"].iloc[0],
            "ATM Strike": day_df["Strike"].iloc[0],
            "Combined Premium": day_df["Combined Premium"].iloc[0],
            "Stop Loss %": stop_loss_pct,
            "Total Trades": len(day_df),
            "Winning Trades": int(len(wins)),
            "Losing Trades": int(len(losses)),
            "Day PnL": float(pnl.sum()),
            "Gross Profit": float(wins.sum()),
            "Gross Loss": float(losses.sum()),
            "Max Drawdown": max_drawdown(equity) if len(equity) else 0.0,
            "Maximum Intraday Profit": float(equity.max()) if len(equity) else 0.0,
            "Maximum Intraday Loss": float(equity.min()) if len(equity) else 0.0,
            "Return %": float(pnl.sum() / abs(day_df["Combined Premium"].iloc[0] * day_df["Strike"].iloc[0])) * 100
                        if day_df["Combined Premium"].iloc[0] else 0.0,
        })
    return pd.DataFrame(rows)


def build_parameter_summary(per_sl_trade_logs: dict[float, pd.DataFrame], starting_capital: float = 1_000_000.0) -> pd.DataFrame:
    rows = []
    for sl_pct, tl in per_sl_trade_logs.items():
        renamed = tl.rename(columns={"Net PnL": "net_pnl"}) if not tl.empty else tl
        m = summarize_trades(renamed, starting_capital=starting_capital)
        rows.append({
            "Stop Loss %": sl_pct,
            "Net Profit": m["net_profit"],
            "Gross Profit": m["gross_profit"],
            "Gross Loss": m["gross_loss"],
            "Profit Factor": m["profit_factor"],
            "Win Rate": m["win_rate"],
            "Average Winner": m["avg_winner"],
            "Average Loser": m["avg_loser"],
            "Expectancy": m["expectancy"],
            "Total Trades": m["total_trades"],
            "Maximum Drawdown": m["max_drawdown"],
            "CAGR": m["cagr"],
            "Sharpe Ratio": m["sharpe"],
            "Sortino Ratio": m["sortino"],
            "Calmar Ratio": m["calmar"],
            "Average Daily Return": m["avg_daily_return"],
            "Maximum Winning Streak": m["max_win_streak"],
            "Maximum Losing Streak": m["max_loss_streak"],
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Net Profit", "Profit Factor"], ascending=False).reset_index(drop=True)
    return df


def export_reports(
    trade_log: pd.DataFrame,
    daily_summary: pd.DataFrame,
    parameter_summary: pd.DataFrame,
    output_dir: str,
) -> dict[str, str]:
    """Writes CSV + Excel for each report. Returns dict of report_name -> path (xlsx)."""
    os.makedirs(os.path.join(output_dir, "trade_logs"), exist_ok=True)
    os.makedirs(os.path.join(output_dir, "reports"), exist_ok=True)

    paths = {}
    specs = [
        ("trade_log", trade_log, os.path.join(output_dir, "trade_logs")),
        ("daily_summary", daily_summary, os.path.join(output_dir, "reports")),
        ("parameter_summary", parameter_summary, os.path.join(output_dir, "reports")),
    ]
    for name, df, folder in specs:
        csv_path = os.path.join(folder, f"{name}.csv")
        xlsx_path = os.path.join(folder, f"{name}.xlsx")
        df.to_csv(csv_path, index=False)
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=name[:31])
        paths[name] = xlsx_path
    return paths
