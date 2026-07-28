"""
Audit report output.

Outputs:
  - Human-readable table to console (uses rich if available, plain text otherwise)
  - audit_report.json to disk
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import structlog

from app.audit.checks import AuditReport
from app.config import get_settings

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def print_report(report: AuditReport) -> None:
    """Print the audit report to stdout."""
    try:
        _print_rich(report)
    except ImportError:
        _print_plain(report)


def _print_rich(report: AuditReport) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()

    # --- Per-underlying summary ---
    table = Table(title="Audit Summary — Per Underlying", box=box.SIMPLE_HEAVY)
    table.add_column("Underlying", style="cyan")
    table.add_column("Pass/Fail", style="bold")
    table.add_column("Strike Issues", justify="right")
    table.add_column("Gap Issues", justify="right")

    for symbol, data in sorted(report.underlying_results.items()):
        pf = data["pass_fail"]
        color = "green" if pf == "PASS" else "red"
        table.add_row(
            symbol,
            f"[{color}]{pf}[/{color}]",
            str(data["strike_issues"]),
            str(data["gap_issues"]),
        )
    console.print(table)

    # --- Strike issues ---
    if report.strike_issues:
        t2 = Table(title="Missing Strikes / CE-PE Pairs", box=box.SIMPLE)
        t2.add_column("Underlying")
        t2.add_column("Expiry")
        t2.add_column("Missing Strikes")
        t2.add_column("Missing CE")
        t2.add_column("Missing PE")
        for issue in report.strike_issues:
            t2.add_row(
                issue.underlying_symbol,
                str(issue.expiry),
                _fmt_list(issue.missing_strikes),
                _fmt_list(issue.missing_ce),
                _fmt_list(issue.missing_pe),
            )
        console.print(t2)

    # --- Gap issues ---
    if report.gap_issues:
        t3 = Table(title="Time-Series Gaps", box=box.SIMPLE)
        t3.add_column("Ticker")
        t3.add_column("Gap Count", justify="right")
        t3.add_column("Largest Gap (min)", justify="right")
        t3.add_column("Gap %", justify="right")
        for issue in report.gap_issues:
            t3.add_row(
                issue.raw_ticker,
                str(issue.gap_count),
                f"{issue.largest_gap_minutes:.1f}",
                f"{issue.gap_pct:.1f}%",
            )
        console.print(t3)

    # --- OI issues ---
    if report.oi_issues:
        t4 = Table(title="Missing Open Interest", box=box.SIMPLE)
        t4.add_column("Ticker")
        t4.add_column("Null OI Bars", justify="right")
        t4.add_column("Total Bars", justify="right")
        for issue in report.oi_issues:
            t4.add_row(issue.raw_ticker, str(issue.null_oi_count), str(issue.total_bars))
        console.print(t4)

    # --- Orphans ---
    if report.orphan_issues:
        t5 = Table(title="Orphan / Integrity Issues", box=box.SIMPLE)
        t5.add_column("Kind")
        t5.add_column("Details")
        for issue in report.orphan_issues:
            t5.add_row(issue.kind, issue.details)
        console.print(t5)

    # --- Feature coverage ---
    if report.feature_issues:
        t6 = Table(title="Feature Coverage Gaps", box=box.SIMPLE)
        t6.add_column("Ticker")
        t6.add_column("Bars", justify="right")
        t6.add_column("VWAP Rows", justify="right")
        for issue in report.feature_issues:
            t6.add_row(issue.raw_ticker, str(issue.bars_count), str(issue.vwap_count))
        console.print(t6)

    # --- Critical failures ---
    if report.critical_failures:
        console.print("\n[bold red]CRITICAL FAILURES:[/bold red]")
        for f in report.critical_failures:
            console.print(f"  [red]✗[/red] {f}")
    else:
        console.print("\n[bold green]All critical checks PASSED.[/bold green]")


def _print_plain(report: AuditReport) -> None:
    """Plain-text fallback (no rich dependency)."""
    print("\n=== Audit Summary — Per Underlying ===")
    print(f"{'Underlying':<30} {'Pass/Fail':<10} {'Strike Issues':>14} {'Gap Issues':>10}")
    print("-" * 68)
    for symbol, data in sorted(report.underlying_results.items()):
        print(
            f"{symbol:<30} {data['pass_fail']:<10} "
            f"{data['strike_issues']:>14} {data['gap_issues']:>10}"
        )

    if report.strike_issues:
        print("\n=== Missing Strikes / CE-PE Pairs ===")
        for issue in report.strike_issues:
            print(
                f"  {issue.underlying_symbol} expiry={issue.expiry} "
                f"missing_strikes={_fmt_list(issue.missing_strikes)} "
                f"missing_ce={_fmt_list(issue.missing_ce)} "
                f"missing_pe={_fmt_list(issue.missing_pe)}"
            )

    if report.gap_issues:
        print("\n=== Time-Series Gaps ===")
        for issue in report.gap_issues:
            print(
                f"  {issue.raw_ticker} gaps={issue.gap_count} "
                f"largest={issue.largest_gap_minutes:.1f}min "
                f"gap_pct={issue.gap_pct:.1f}%"
            )

    if report.oi_issues:
        print("\n=== Missing Open Interest ===")
        for issue in report.oi_issues:
            print(f"  {issue.raw_ticker} null_oi={issue.null_oi_count}/{issue.total_bars}")

    if report.orphan_issues:
        print("\n=== Orphan / Integrity Issues ===")
        for issue in report.orphan_issues:
            print(f"  [{issue.kind}] {issue.details}")

    if report.feature_issues:
        print("\n=== Feature Coverage Gaps ===")
        for issue in report.feature_issues:
            print(f"  {issue.raw_ticker} bars={issue.bars_count} vwap={issue.vwap_count}")

    if report.critical_failures:
        print("\n!!! CRITICAL FAILURES !!!")
        for f in report.critical_failures:
            print(f"  ✗ {f}")
    else:
        print("\nAll critical checks PASSED.")


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def write_json_report(report: AuditReport, output_path: str | Path | None = None) -> Path:
    """Write audit_report.json and return its path."""
    if output_path is None:
        output_path = get_settings().audit.output_path

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = report.to_dict()
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)

    logger.info("audit_report_written", path=str(path))
    print(f"\nAudit report written to: {path.resolve()}")
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_list(items: list[Any], max_items: int = 10) -> str:
    if not items:
        return "-"
    strs = [str(x) for x in items[:max_items]]
    if len(items) > max_items:
        strs.append(f"…+{len(items) - max_items}")
    return ", ".join(strs)
