"""
Batched file reader using polars for CSV, Excel, and pickle-backed DataFrames.

Returns an iterator of polars DataFrames (one per batch).

For CSV: uses polars LazyFrame + streaming collect in batches.
For XLSX: loads the whole file (no streaming API), then chunks into batches.
For pickle: loads a pandas DataFrame or Series from disk and converts it to polars.
"""
from __future__ import annotations

import math
import pickle
from pathlib import Path
from typing import Iterator

import polars as pl
import structlog

logger = structlog.get_logger(__name__)

_SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pkl", ".pickle"}


def read_file_batches(
    file_path: str | Path,
    batch_size: int = 10_000,
) -> Iterator[pl.DataFrame]:
    """
    Yield polars DataFrames of up to `batch_size` rows from the source file.

    Supported formats:
      - .csv  — polars streaming reader
      - .xlsx / .xls — polars.read_excel (falls back to pandas if unavailable)

    All column names are preserved as-is from the file; normalisation happens
    in column_mapper.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")

    ext = path.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension {ext!r}. Supported: {_SUPPORTED_EXTENSIONS}")

    logger.info("reading_file", path=str(path), ext=ext, batch_size=batch_size)

    if ext == ".csv":
        yield from _read_csv_batches(path, batch_size)
    elif ext in {".xlsx", ".xls"}:
        yield from _read_excel_batches(path, batch_size)
    elif ext in {".pkl", ".pickle"}:
        yield from _read_pickle_batches(path, batch_size)
    else:
        raise ValueError(f"Unsupported file extension {ext!r}. Supported: {_SUPPORTED_EXTENSIONS}")


# ---------------------------------------------------------------------------
# CSV reader
# ---------------------------------------------------------------------------

def _read_csv_batches(path: Path, batch_size: int) -> Iterator[pl.DataFrame]:
    """Read a CSV in chunks using polars scan_csv (lazy streaming)."""
    # Read entire CSV lazily; collect in slices to avoid loading all into RAM.
    # polars has a read_csv with batch_size for streaming; we use that approach.
    try:
        reader = pl.read_csv_batched(
            path,
            batch_size=batch_size,
            infer_schema_length=0,   # keep everything as Utf8 for safety
            ignore_errors=False,
            truncate_ragged_lines=True,
        )
        while True:
            batch = reader.next_batches(1)
            if not batch:
                break
            df = batch[0]
            if df.is_empty():
                break
            logger.debug("csv_batch", rows=len(df))
            yield df
    except Exception as exc:
        logger.error("csv_read_error", path=str(path), error=str(exc))
        raise


# ---------------------------------------------------------------------------
# Excel reader
# ---------------------------------------------------------------------------

def _read_excel_batches(path: Path, batch_size: int) -> Iterator[pl.DataFrame]:
    """
    Read an Excel file.  Tries polars.read_excel first; falls back to pandas.
    Yields chunks of `batch_size` rows.
    """
    df = _load_excel(path)
    total = len(df)
    num_batches = max(1, math.ceil(total / batch_size))
    logger.info("excel_loaded", path=str(path), total_rows=total, batches=num_batches)

    for i in range(num_batches):
        start = i * batch_size
        end = min(start + batch_size, total)
        yield df.slice(start, end - start)


def _load_excel(path: Path) -> pl.DataFrame:
    """Load an Excel file into a polars DataFrame."""
    try:
        df = pl.read_excel(path)
        # Cast all columns to String for uniform downstream processing
        df = df.with_columns([pl.col(c).cast(pl.Utf8) for c in df.columns])
        return df
    except Exception as polars_exc:
        logger.warning(
            "polars_excel_failed_fallback_to_pandas",
            path=str(path),
            error=str(polars_exc),
        )

    # Pandas fallback
    try:
        import pandas as pd
        pdf = pd.read_excel(path, dtype=str)
        df = pl.from_pandas(pdf)
        return df
    except Exception as pandas_exc:
        raise RuntimeError(
            f"Failed to read Excel file {path}: polars error: {polars_exc}; "
            f"pandas error: {pandas_exc}"
        ) from pandas_exc


def _read_pickle_batches(path: Path, batch_size: int) -> Iterator[pl.DataFrame]:
    """Load a pickle file containing a pandas DataFrame/Series and yield batched polars DataFrames."""
    try:
        with path.open("rb") as handle:
            obj = pickle.load(handle)
    except Exception as exc:
        logger.error("pickle_read_error", path=str(path), error=str(exc))
        raise

    if hasattr(obj, "to_pandas"):
        pdf = obj.to_pandas()
    elif hasattr(obj, "to_frame") and not hasattr(obj, "columns"):
        pdf = obj.to_frame()
    elif hasattr(obj, "columns"):
        pdf = obj
    else:
        raise TypeError(f"Unsupported pickle object type: {type(obj)!r}")

    if hasattr(pdf, "columns"):
        try:
            df = pl.from_pandas(pdf)
        except ImportError:
            columns = {col: pdf[col].astype(str).to_list() for col in pdf.columns}
            df = pl.DataFrame(columns)
        df = df.with_columns([pl.col(c).cast(pl.Utf8) for c in df.columns])
    else:
        raise TypeError(f"Unsupported pickle content type: {type(pdf)!r}")

    total = len(df)
    num_batches = max(1, math.ceil(total / batch_size))
    logger.info("pickle_loaded", path=str(path), total_rows=total, batches=num_batches)

    for i in range(num_batches):
        start = i * batch_size
        end = min(start + batch_size, total)
        yield df.slice(start, end - start)


def detect_file_type(path: str | Path) -> str:
    """Return 'csv', 'excel', or 'pickle' based on file extension."""
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return "csv"
    if ext in (".xlsx", ".xls"):
        return "excel"
    if ext in {".pkl", ".pickle"}:
        return "pickle"
    raise ValueError(f"Unknown file type for extension {ext!r}")
