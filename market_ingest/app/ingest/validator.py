"""
Row-level validation of OHLCV bars.

Each row is checked against:
  1. high >= low
  2. open, high, low, close, volume >= 0
  3. Timestamp is a valid datetime (already parsed upstream)

Invalid rows are collected into a RejectReport rather than aborting the file,
unless `strict=True` in which case the first violation raises ValueError.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RowReject:
    """Records a single rejected row with its reason."""
    row_index: int
    raw_ticker: str
    ts: str
    reason: str
    raw_values: dict[str, Any]


@dataclass
class RejectReport:
    """Accumulates all row-level rejects from a file."""
    rejects: list[RowReject] = field(default_factory=list)

    def add(self, reject: RowReject) -> None:
        self.rejects.append(reject)

    @property
    def count(self) -> int:
        return len(self.rejects)

    def is_empty(self) -> bool:
        return not self.rejects

    def summary(self) -> str:
        if not self.rejects:
            return "No row-level rejects."
        lines = [f"Row-level rejects: {self.count}"]
        for r in self.rejects[:20]:  # cap display at 20
            lines.append(f"  row={r.row_index} ticker={r.raw_ticker} ts={r.ts} reason={r.reason}")
        if self.count > 20:
            lines.append(f"  ... and {self.count - 20} more.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_row(
    row_index: int,
    raw_ticker: str,
    ts: Any,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    open_interest: float | None,
    strict: bool = False,
    reject_report: RejectReport | None = None,
) -> bool:
    """
    Validate a single OHLCV row.

    Returns True if valid, False if rejected.
    In strict mode raises ValueError on the first violation.
    Otherwise appends to reject_report (if provided).
    """
    ts_str = str(ts)
    raw = {
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
        "open_interest": open_interest,
    }

    errors: list[str] = []

    # Non-negative checks
    for name, val in [("open", open_), ("high", high), ("low", low),
                      ("close", close), ("volume", volume)]:
        if val is None:
            errors.append(f"{name} is None")
        elif val < 0:
            errors.append(f"{name}={val} < 0")

    # high >= low
    if high is not None and low is not None and high < low:
        errors.append(f"high={high} < low={low}")

    if not errors:
        return True

    reason = "; ".join(errors)
    reject = RowReject(
        row_index=row_index,
        raw_ticker=raw_ticker,
        ts=ts_str,
        reason=reason,
        raw_values=raw,
    )

    if strict:
        raise ValueError(
            f"Row {row_index} ticker={raw_ticker} ts={ts_str} failed validation: {reason}"
        )

    if reject_report is not None:
        reject_report.add(reject)

    return False


def validate_batch(
    rows: list[dict[str, Any]],
    strict: bool = False,
    reject_report: RejectReport | None = None,
) -> list[dict[str, Any]]:
    """
    Validate a list of row dicts.  Returns the list of valid rows.
    Invalid rows are added to reject_report (or raise in strict mode).

    Each dict is expected to have keys:
        raw_ticker, ts, open, high, low, close, volume, open_interest (opt)
        row_index (int)
    """
    valid: list[dict[str, Any]] = []
    for row in rows:
        ok = validate_row(
            row_index=row.get("row_index", 0),
            raw_ticker=row.get("raw_ticker", ""),
            ts=row.get("ts"),
            open_=_to_float(row.get("open")),
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=_to_float(row.get("close")),
            volume=_to_float(row.get("volume")),
            open_interest=_to_float_opt(row.get("open_interest")),
            strict=strict,
            reject_report=reject_report,
        )
        if ok:
            valid.append(row)
    return valid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(val: Any) -> float:
    if val is None or val == "" or val != val:  # NaN check
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _to_float_opt(val: Any) -> float | None:
    if val is None or val == "" or (isinstance(val, float) and val != val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
