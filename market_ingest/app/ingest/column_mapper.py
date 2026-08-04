"""
Column mapper — normalises source CSV/Excel headers to canonical field names
using a YAML profile.

Canonical fields
----------------
Required (always): ticker, date, open, high, low, close
Required (intraday): time
Optional: volume, open_interest

Rules
-----
- Headers are lowercased and stripped before matching.
- Each source header may match at most one canonical field.
- Each required canonical field must match exactly one source header.
- Ambiguity (one header matching two fields, or a field matched by multiple
  headers when that is unexpected) raises ColumnMapError.
- Missing required field → ColumnMapError (file is rejected).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import structlog

from app.config import ColumnMapProfile

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class ColumnMapError(ValueError):
    """Raised when headers cannot be unambiguously mapped to canonical fields."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class MappingResult:
    """Maps canonical field names to actual source column names."""
    # canonical → source column name (as it appears in the file)
    field_to_source: dict[str, str] = field(default_factory=dict)
    # source column name → canonical field
    source_to_field: dict[str, str] = field(default_factory=dict)

    def get(self, canonical: str) -> str | None:
        """Return the source column name for a canonical field, or None."""
        return self.field_to_source.get(canonical)

    def require(self, canonical: str) -> str:
        """Return the source column name or raise ColumnMapError."""
        col = self.field_to_source.get(canonical)
        if col is None:
            raise ColumnMapError(f"Required canonical field {canonical!r} has no mapping")
        return col


# ---------------------------------------------------------------------------
# Core mapper
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = frozenset({"ticker", "date", "open", "high", "low", "close"})
_REQUIRED_INTRADAY = frozenset({"time"})
_OPTIONAL_FIELDS = frozenset({"volume", "open_interest"})


def _is_combined_datetime_header(header: str) -> bool:
    """Return whether a date column header represents a full date-time value."""
    normalised = " ".join(header.strip().lower().replace("_", " ").replace("-", " ").split())
    return normalised in {"date time", "datetime", "timestamp", "date and time"}


def map_columns(
    source_headers: Sequence[str],
    profile: ColumnMapProfile,
) -> MappingResult:
    """
    Map source headers to canonical field names using the given profile.

    Parameters
    ----------
    source_headers:
        Column names as they appear in the source file (case-preserved).
    profile:
        Loaded ColumnMapProfile from YAML.

    Returns
    -------
    MappingResult with bidirectional mapping.

    Raises
    ------
    ColumnMapError on any ambiguity or missing required field.
    """
    # Build lookup: alias (lower-stripped) → canonical
    alias_to_canonical: dict[str, str] = {}
    for canonical, aliases in profile.column_map.items():
        for alias in aliases:
            alias_norm = alias.strip().lower()
            if alias_norm in alias_to_canonical:
                existing = alias_to_canonical[alias_norm]
                if existing != canonical:
                    raise ColumnMapError(
                        f"Alias {alias!r} appears in both {existing!r} and {canonical!r} "
                        f"in the profile — fix the profile."
                    )
            alias_to_canonical[alias_norm] = canonical

    result = MappingResult()
    # Track which source headers were claimed (normalised → original)
    claimed_source: dict[str, str] = {}  # canonical → original source header

    for src_header in source_headers:
        norm = src_header.strip().lower()
        canonical = alias_to_canonical.get(norm)
        if canonical is None:
            logger.debug("unmapped_column", source_header=src_header)
            continue

        # Check if this canonical was already claimed by a different source header
        if canonical in claimed_source:
            raise ColumnMapError(
                f"Canonical field {canonical!r} is matched by two source headers: "
                f"{claimed_source[canonical]!r} and {src_header!r}. "
                f"Cannot resolve ambiguity."
            )

        claimed_source[canonical] = src_header
        result.field_to_source[canonical] = src_header
        result.source_to_field[src_header] = canonical

    # Check required fields
    required = set(_REQUIRED_FIELDS)
    if profile.granularity == "intraday":
        required |= _REQUIRED_INTRADAY

        # Intraday sources may provide one combined timestamp column instead
        # of separate date and time columns.  The pipeline already parses a
        # full datetime value from the mapped date column when no time column
        # is present, so do not reject this valid layout at the mapping stage.
        date_source = result.get("date")
        if date_source and _is_combined_datetime_header(date_source):
            required.discard("time")

    missing = required - set(result.field_to_source.keys())
    if missing:
        raise ColumnMapError(
            f"Required field(s) {sorted(missing)} could not be mapped from "
            f"source headers {list(source_headers)}. "
            f"Update the profile column_map or the source file."
        )

    logger.info(
        "column_mapping_complete",
        mapped=list(result.field_to_source.keys()),
        unmapped=[h for h in source_headers if h not in result.source_to_field],
    )
    return result


def describe_mapping(result: MappingResult) -> str:
    """Return a human-readable description of the mapping."""
    lines = ["Column mapping:"]
    for canonical, source in sorted(result.field_to_source.items()):
        lines.append(f"  {canonical:20s} ← {source!r}")
    return "\n".join(lines)
