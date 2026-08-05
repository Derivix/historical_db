from __future__ import annotations

from pathlib import Path
import sys

import typer

from app.ingest.pipeline import ingest_path

app = typer.Typer(add_completion=False)


@app.command()
def main(
    data_dir: Path = typer.Option(
        Path("data"),
        "--data-dir",
        "-d",
        help="Directory containing CSV/XLSX files to ingest.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        "-p",
        help="Source profile name to use for column mapping.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Abort on first row validation error.",
    ),
) -> None:
    """Ingest all supported files from the data directory."""
    data_dir = data_dir if data_dir.is_absolute() else Path.cwd() / data_dir

    if not data_dir.exists():
        typer.echo(f"Data directory does not exist: {data_dir}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Ingesting all files from: {data_dir}")
    result = ingest_path(data_dir, profile_name=profile, strict=strict)
    typer.echo(result.summary())

    if result.errors:
        typer.echo("Errors occurred during ingest:", err=True)
        for error in result.errors:
            typer.echo(f"  {error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
