"""
CLI entry point — typer-based.

Commands:
  migrate         Create/upgrade database schema idempotently
  ingest <path>   Load one file or directory
  features        Compute VWAP + OFFSET selections
  audit           Run completeness checks
  all <path>      migrate → ingest → features → audit
"""
from __future__ import annotations

import sys
import logging
from pathlib import Path
from typing import Optional

import structlog
import typer

# Configure structlog early
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
)

app = typer.Typer(
    name="market-ingest",
    help="Market data ingestion system for NFO segment OHLCV data.",
    add_completion=False,
)


@app.command()
def migrate(
    dsn: Optional[str] = typer.Option(None, "--dsn", help="PostgreSQL DSN (overrides config)")
) -> None:
    """Create or upgrade the database schema idempotently."""
    from app.db.migrations import run_migrations
    typer.echo("Running migrations…")
    try:
        run_migrations(dsn=dsn)
        typer.echo("Migrations complete.")
    except Exception as exc:
        typer.echo(f"Migration failed: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def ingest(
    path: str = typer.Argument(..., help="Path to a CSV/XLSX file or directory"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Source profile name"),
    strict: bool = typer.Option(False, "--strict", help="Abort on first row validation error"),
) -> None:
    """Load a file or directory of files into the database."""
    from app.ingest.pipeline import ingest_path
    typer.echo(f"Ingesting: {path}")
    try:
        result = ingest_path(path, profile_name=profile, strict=strict)
        typer.echo(result.summary())
        if result.errors:
            typer.echo("Errors:", err=True)
            for e in result.errors:
                typer.echo(f"  {e}", err=True)
            raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def features(
    method: str = typer.Option("OFFSET", "--method", "-m", help="Selection method (default: OFFSET)"),
) -> None:
    """Compute VWAP features and option selections."""
    from app.features.vwap import compute_and_store_vwap
    from app.features.selection import run_selections

    typer.echo("Computing VWAP…")
    try:
        n_vwap = compute_and_store_vwap()
        typer.echo(f"VWAP rows stored: {n_vwap}")
    except Exception as exc:
        typer.echo(f"VWAP computation failed: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Running {method} selections…")
    try:
        n_sel = run_selections(method=method)
        typer.echo(f"Selection rows stored: {n_sel}")
    except Exception as exc:
        typer.echo(f"Selection computation failed: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def audit(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Path for JSON report"),
) -> None:
    """
    Run completeness and integrity checks.

    Exits with code 1 if any critical check fails.
    """
    from app.audit.checks import run_all_checks
    from app.audit.report import print_report, write_json_report

    typer.echo("Running audit checks…")
    try:
        report = run_all_checks()
    except Exception as exc:
        typer.echo(f"Audit failed: {exc}", err=True)
        raise typer.Exit(code=1)

    print_report(report)
    write_json_report(report, output_path=output)

    if report.has_critical_failures():
        raise typer.Exit(code=1)


@app.command(name="all")
def run_all(
    path: str = typer.Argument(..., help="Path to a CSV/XLSX file or directory"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Source profile name"),
    strict: bool = typer.Option(False, "--strict", help="Abort on first row validation error"),
    method: str = typer.Option("OFFSET", "--method", "-m", help="Selection method"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Audit JSON output path"),
) -> None:
    """
    Run migrate → ingest → features → audit in sequence.

    Exits non-zero if any step fails or audit finds critical issues.
    """
    from app.db.migrations import run_migrations
    from app.ingest.pipeline import ingest_path
    from app.features.vwap import compute_and_store_vwap
    from app.features.selection import run_selections
    from app.audit.checks import run_all_checks
    from app.audit.report import print_report, write_json_report

    typer.echo("Step 1/4: Running migrations…")
    try:
        run_migrations()
    except Exception as exc:
        typer.echo(f"Migration failed: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Step 2/4: Ingesting {path}…")
    try:
        result = ingest_path(path, profile_name=profile, strict=strict)
        typer.echo(result.summary())
        if result.errors:
            for e in result.errors:
                typer.echo(f"  [error] {e}", err=True)
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo("Step 3/4: Computing features…")
    try:
        n_vwap = compute_and_store_vwap()
        typer.echo(f"  VWAP rows: {n_vwap}")
        n_sel = run_selections(method=method)
        typer.echo(f"  Selection rows: {n_sel}")
    except Exception as exc:
        typer.echo(f"Features step failed: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo("Step 4/4: Running audit…")
    try:
        report = run_all_checks()
    except Exception as exc:
        typer.echo(f"Audit failed: {exc}", err=True)
        raise typer.Exit(code=1)

    print_report(report)
    write_json_report(report, output_path=output)

    if report.has_critical_failures():
        raise typer.Exit(code=1)

    typer.echo("\nAll steps complete.")


if __name__ == "__main__":
    app()
