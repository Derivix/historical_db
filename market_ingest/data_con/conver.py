#!/usr/bin/env python3
"""
Bulk CSV -> Parquet converter.

Designed for OHLCV-style tick/candle data like:

Ticker,Date,Time,Open,High,Low,Close,Volume,Open Interest
360ONE-I.NFO,02/01/2026,09:16:59,1178.5,1178.5,1174.6,1174.6,500,2249000

Features
--------
- Converts every .csv file in an input folder to a .parquet file.
- Optionally merges all CSVs into a single partitioned Parquet dataset
  (partitioned by Ticker and/or Date) for fast downstream querying.
- Combines Date + Time into a proper timestamp column.
- Uses efficient dtypes and Snappy compression.
- Processes files in chunks to keep memory usage low on large files.

Usage
-----
# Convert each CSV to its own parquet file (1:1), output next to input:
python csv_to_parquet.py --input ./csv_data --output ./parquet_data

# Merge everything into one partitioned dataset (recommended for querying):
python csv_to_parquet.py --input ./csv_data --output ./parquet_data --merge --partition-by Ticker

# Change chunk size (rows) for very large files:
python csv_to_parquet.py --input ./csv_data --output ./parquet_data --chunksize 500000
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


# ---- Fixed schema knowledge for this dataset -------------------------------

DTYPES = {
    "Ticker": "string",
    "Open": "float32",
    "High": "float32",
    "Low": "float32",
    "Close": "float32",
    "Volume": "int64",
    "Open Interest": "int64",
}

DATE_FMT = "%d/%m/%Y"
TIME_FMT = "%H:%M:%S"


def read_csv_chunks(csv_path: Path, chunksize: int):
    """Yield cleaned DataFrame chunks from a CSV file."""
    reader = pd.read_csv(
        csv_path,
        dtype=DTYPES,
        chunksize=chunksize,
    )
    for chunk in reader:
        yield clean_chunk(chunk)


def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
    """Combine Date+Time into a timestamp column and tidy up dtypes."""
    # Build a single datetime column from Date + Time
    df["Timestamp"] = pd.to_datetime(
        df["Date"] + " " + df["Time"],
        format=f"{DATE_FMT} {TIME_FMT}",
        errors="coerce",
    )

    # Keep the parsed Date as a proper date type too (handy for partitioning)
    df["Date"] = pd.to_datetime(df["Date"], format=DATE_FMT, errors="coerce").dt.date

    # Reorder columns: Timestamp first, drop the raw Time column
    cols = ["Ticker", "Timestamp", "Date", "Open", "High", "Low", "Close", "Volume", "Open Interest"]
    df = df[cols]

    return df


def convert_one_to_one(input_dir: Path, output_dir: Path, chunksize: int):
    """Convert each CSV file into its own Parquet file."""
    csv_files = sorted(input_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {input_dir}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    for csv_path in csv_files:
        out_path = output_dir / (csv_path.stem + ".parquet")
        print(f"Converting {csv_path.name} -> {out_path.name}")

        writer = None
        try:
            for chunk in read_csv_chunks(csv_path, chunksize):
                table = pa.Table.from_pandas(chunk, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(out_path, table.schema, compression="snappy")
                writer.write_table(table)
        finally:
            if writer is not None:
                writer.close()

    print(f"\nDone. {len(csv_files)} file(s) converted into: {output_dir}")


def convert_merged_dataset(input_dir: Path, output_dir: Path, chunksize: int, partition_cols):
    """Merge all CSVs into a single partitioned Parquet dataset."""
    csv_files = sorted(input_dir.glob("*.csv"))
    if not csv_files:
        print(f"No CSV files found in {input_dir}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    for csv_path in csv_files:
        print(f"Processing {csv_path.name} ...")
        for chunk in read_csv_chunks(csv_path, chunksize):
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            pq.write_to_dataset(
                table,
                root_path=str(output_dir),
                partition_cols=partition_cols if partition_cols else None,
                compression="snappy",
                existing_data_behavior="overwrite_or_ignore",
            )
            total_rows += len(chunk)

    print(f"\nDone. {total_rows:,} rows from {len(csv_files)} file(s) written to: {output_dir}")
    if partition_cols:
        print(f"Partitioned by: {', '.join(partition_cols)}")


def main():
    parser = argparse.ArgumentParser(description="Bulk convert CSV files to Parquet.")
    parser.add_argument("--input", required=True, help="Folder containing .csv files")
    parser.add_argument("--output", required=True, help="Folder to write .parquet output to")
    parser.add_argument(
        "--chunksize", type=int, default=1_000_000,
        help="Rows per chunk when reading large CSVs (default: 1,000,000)",
    )
    parser.add_argument(
        "--merge", action="store_true",
        help="Merge all CSVs into a single partitioned Parquet dataset instead of 1:1 files",
    )
    parser.add_argument(
        "--partition-by", nargs="*", default=["Ticker"],
        help="Columns to partition by when --merge is used (default: Ticker). "
             "Pass 'none' to disable partitioning, e.g. --partition-by none",
    )

    args = parser.parse_args()

    input_dir = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()

    if not input_dir.is_dir():
        print(f"Input folder not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    if args.merge:
        partition_cols = None if [c.lower() for c in args.partition_by] == ["none"] else args.partition_by
        convert_merged_dataset(input_dir, output_dir, args.chunksize, partition_cols)
    else:
        convert_one_to_one(input_dir, output_dir, args.chunksize)


if __name__ == "__main__":
    main()