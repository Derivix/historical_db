"""
Completeness and integrity audit checks.

Checks implemented:
  1. Instrument coverage  — missing strikes, missing CE/PE pairs
  2. Time-series gaps     — missing bars vs expected trading grid
  3. Missing Open Interest
  4. Orphans / integrity  — bars without parent, options without spot
  5. Feature coverage     — bars with no computed VWAP
  6. Summary              — pass/fail per underlying
"""
from __future__ import annotations

import zoneinfo
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog

from app.config import get_settings
from app.db.connection import get_connection

logger = structlog.get_logger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class StrikeCoverageIssue:
    underlying_symbol: str
    expiry: date
    missing_strikes: list[Decimal]
    missing_ce: list[Decimal]
    missing_pe: list[Decimal]


@dataclass
class GapIssue:
    instrument_id: int
    raw_ticker: str
    gap_count: int
    largest_gap_minutes: float
    gap_pct: float


@dataclass
class OIIssue:
    instrument_id: int
    raw_ticker: str
    null_oi_count: int
    total_bars: int


@dataclass
class OrphanIssue:
    kind: str   # "bar_without_instrument" | "option_without_spot" | "selection_no_bars"
    details: str


@dataclass
class FeatureCoverageIssue:
    instrument_id: int
    raw_ticker: str
    bars_count: int
    vwap_count: int


@dataclass
class AuditReport:
    """Aggregated result from all audit checks."""
    strike_issues: list[StrikeCoverageIssue] = field(default_factory=list)
    gap_issues: list[GapIssue] = field(default_factory=list)
    oi_issues: list[OIIssue] = field(default_factory=list)
    orphan_issues: list[OrphanIssue] = field(default_factory=list)
    feature_issues: list[FeatureCoverageIssue] = field(default_factory=list)
    underlying_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    critical_failures: list[str] = field(default_factory=list)

    def has_critical_failures(self) -> bool:
        return bool(self.critical_failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strike_issues": [
                {
                    "underlying": i.underlying_symbol,
                    "expiry": str(i.expiry),
                    "missing_strikes": [float(s) for s in i.missing_strikes],
                    "missing_ce_strikes": [float(s) for s in i.missing_ce],
                    "missing_pe_strikes": [float(s) for s in i.missing_pe],
                }
                for i in self.strike_issues
            ],
            "gap_issues": [
                {
                    "instrument_id": i.instrument_id,
                    "raw_ticker": i.raw_ticker,
                    "gap_count": i.gap_count,
                    "largest_gap_minutes": i.largest_gap_minutes,
                    "gap_pct": i.gap_pct,
                }
                for i in self.gap_issues
            ],
            "oi_issues": [
                {
                    "instrument_id": i.instrument_id,
                    "raw_ticker": i.raw_ticker,
                    "null_oi_count": i.null_oi_count,
                    "total_bars": i.total_bars,
                }
                for i in self.oi_issues
            ],
            "orphan_issues": [
                {"kind": i.kind, "details": i.details}
                for i in self.orphan_issues
            ],
            "feature_issues": [
                {
                    "instrument_id": i.instrument_id,
                    "raw_ticker": i.raw_ticker,
                    "bars_count": i.bars_count,
                    "vwap_count": i.vwap_count,
                }
                for i in self.feature_issues
            ],
            "underlying_results": self.underlying_results,
            "critical_failures": self.critical_failures,
        }


# ---------------------------------------------------------------------------
# Check 1: Instrument / strike coverage
# ---------------------------------------------------------------------------

def check_instrument_coverage(conn: Any) -> list[StrikeCoverageIssue]:
    """
    For each underlying with options: for each expiry, compute expected strike
    ladder (min to max by strike_step) and flag missing strikes and missing
    CE/PE pairs.
    """
    issues: list[StrikeCoverageIssue] = []

    underlyings = conn.execute(
        "SELECT underlying_id, symbol, strike_step FROM underlying ORDER BY symbol"
    ).fetchall()

    for und_id, symbol, strike_step in underlyings:
        expiries = conn.execute(
            """
            SELECT DISTINCT expiry FROM instrument
            WHERE underlying_id = %s AND expiry IS NOT NULL
            ORDER BY expiry
            """,
            (und_id,),
        ).fetchall()

        for (expiry,) in expiries:
            # Fetch all option instruments for this expiry
            opt_rows = conn.execute(
                """
                SELECT strike, option_type FROM instrument
                WHERE underlying_id = %s AND expiry = %s
                  AND instrument_type IN ('CE', 'PE')
                  AND strike IS NOT NULL AND option_type IS NOT NULL
                ORDER BY strike, option_type
                """,
                (und_id, expiry),
            ).fetchall()

            if not opt_rows:
                continue

            strikes_present: set[Decimal] = set()
            ce_strikes: set[Decimal] = set()
            pe_strikes: set[Decimal] = set()

            for strike, opt_type in opt_rows:
                s = Decimal(str(strike))
                strikes_present.add(s)
                if opt_type == "CE":
                    ce_strikes.add(s)
                else:
                    pe_strikes.add(s)

            # Expected ladder
            missing_strikes: list[Decimal] = []
            if strike_step and strike_step > 0:
                min_s = min(strikes_present)
                max_s = max(strikes_present)
                expected = _build_ladder(min_s, max_s, Decimal(str(strike_step)))
                missing_strikes = sorted(expected - strikes_present)

            # Missing CE/PE pairs among present strikes
            missing_ce = sorted(pe_strikes - ce_strikes)
            missing_pe = sorted(ce_strikes - pe_strikes)

            if missing_strikes or missing_ce or missing_pe:
                issues.append(StrikeCoverageIssue(
                    underlying_symbol=symbol,
                    expiry=expiry,
                    missing_strikes=missing_strikes,
                    missing_ce=missing_ce,
                    missing_pe=missing_pe,
                ))

    return issues


def _build_ladder(min_s: Decimal, max_s: Decimal, step: Decimal) -> set[Decimal]:
    """Generate the full expected strike ladder from min to max by step."""
    result: set[Decimal] = set()
    current = min_s
    while current <= max_s:
        result.add(current)
        current += step
    return result


# ---------------------------------------------------------------------------
# Check 2: Time-series gaps
# ---------------------------------------------------------------------------

def check_time_series_gaps(conn: Any) -> list[GapIssue]:
    """
    For each instrument, detect missing bars within the active window against
    expected trading grid (09:15–15:30 IST, 1-min granularity).
    """
    settings = get_settings()
    session_start = settings.session.start  # "09:15"
    session_end = settings.session.end      # "15:30"
    gran = settings.session.granularity_minutes

    start_h, start_m = map(int, session_start.split(":"))
    end_h, end_m = map(int, session_end.split(":"))

    # Expected bars per day
    expected_per_day = _expected_bars_per_day(start_h, start_m, end_h, end_m, gran)
    max_gap_pct = settings.audit.max_gap_pct

    issues: list[GapIssue] = []

    instruments = conn.execute(
        """
        SELECT i.instrument_id, i.raw_ticker
        FROM instrument i
        WHERE EXISTS (SELECT 1 FROM ohlcv o WHERE o.instrument_id = i.instrument_id)
        ORDER BY i.instrument_id
        """
    ).fetchall()

    for inst_id, raw_ticker in instruments:
        ts_rows = conn.execute(
            "SELECT ts FROM ohlcv WHERE instrument_id = %s ORDER BY ts",
            (inst_id,),
        ).fetchall()

        if len(ts_rows) < 2:
            continue

        timestamps = [r[0] for r in ts_rows]
        gap_count, largest_gap = _find_gaps(timestamps, gran, start_h, start_m, end_h, end_m)

        if gap_count == 0:
            continue

        # Compute expected bars for the active date range
        first_date = timestamps[0].date()
        last_date = timestamps[-1].date()
        trading_days = _count_trading_days(conn, first_date, last_date, inst_id)
        expected_total = trading_days * expected_per_day
        actual = len(timestamps)
        if expected_total > 0:
            gap_pct = 100.0 * gap_count / expected_total
        else:
            gap_pct = 0.0

        if gap_count > 0:
            issues.append(GapIssue(
                instrument_id=inst_id,
                raw_ticker=raw_ticker or str(inst_id),
                gap_count=gap_count,
                largest_gap_minutes=largest_gap,
                gap_pct=gap_pct,
            ))

    return issues


def _expected_bars_per_day(start_h: int, start_m: int, end_h: int, end_m: int, gran: int) -> int:
    """Number of expected 1-min bars in a trading day."""
    total_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    return max(1, total_minutes // gran + 1)  # inclusive


def _find_gaps(
    timestamps: list[datetime],
    gran: int,
    start_h: int, start_m: int,
    end_h: int, end_m: int,
) -> tuple[int, float]:
    """
    Count gaps (consecutive bars more than `gran` minutes apart within a session)
    and return (gap_count, largest_gap_minutes).

    Cross-session gaps (end-of-day to next day) are not counted.
    """
    gap_count = 0
    largest_gap = 0.0

    for i in range(1, len(timestamps)):
        prev = timestamps[i - 1]
        curr = timestamps[i]

        # Skip if it's a cross-session boundary
        # (prev is on a different day or prev is near session end)
        prev_ist = prev.astimezone(IST)
        curr_ist = curr.astimezone(IST)

        if prev_ist.date() != curr_ist.date():
            continue  # cross-day gap, not counted

        diff = (curr - prev).total_seconds() / 60.0
        if diff > gran:
            gap_count += 1
            if diff > largest_gap:
                largest_gap = diff

    return gap_count, largest_gap


def _count_trading_days(
    conn: Any,
    first_date: date,
    last_date: date,
    instrument_id: int,
) -> int:
    """Count distinct trading days with at least one bar for this instrument."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT ts::DATE) FROM ohlcv
        WHERE instrument_id = %s
        """,
        (instrument_id,),
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Check 3: Missing Open Interest
# ---------------------------------------------------------------------------

def check_missing_open_interest(conn: Any) -> list[OIIssue]:
    """Count bars where open_interest IS NULL for options and futures."""
    issues: list[OIIssue] = []

    rows = conn.execute(
        """
        SELECT
            o.instrument_id,
            i.raw_ticker,
            COUNT(*) FILTER (WHERE o.open_interest IS NULL) AS null_oi,
            COUNT(*) AS total
        FROM ohlcv o
        JOIN instrument i ON o.instrument_id = i.instrument_id
        WHERE i.instrument_type IN ('CE', 'PE', 'FUT')
        GROUP BY o.instrument_id, i.raw_ticker
        HAVING COUNT(*) FILTER (WHERE o.open_interest IS NULL) > 0
        ORDER BY null_oi DESC
        """
    ).fetchall()

    for inst_id, raw_ticker, null_oi, total in rows:
        issues.append(OIIssue(
            instrument_id=inst_id,
            raw_ticker=raw_ticker or str(inst_id),
            null_oi_count=null_oi,
            total_bars=total,
        ))

    return issues


# ---------------------------------------------------------------------------
# Check 4: Orphans / integrity
# ---------------------------------------------------------------------------

def check_orphans(conn: Any) -> list[OrphanIssue]:
    """
    Detect:
    - Bars whose instrument_id doesn't exist (shouldn't happen with FK, but check)
    - Options whose underlying spot line has no bars
    - Selections referencing instruments with no bars
    """
    issues: list[OrphanIssue] = []

    # Options without spot bars
    rows = conn.execute(
        """
        SELECT DISTINCT u.symbol
        FROM underlying u
        WHERE EXISTS (
            SELECT 1 FROM instrument i
            WHERE i.underlying_id = u.underlying_id
              AND i.instrument_type IN ('CE', 'PE')
        )
        AND NOT EXISTS (
            SELECT 1 FROM instrument i
            JOIN ohlcv o ON o.instrument_id = i.instrument_id
            WHERE i.underlying_id = u.underlying_id
              AND i.instrument_type IN ('INDEX', 'EQ')
        )
        """
    ).fetchall()
    for (sym,) in rows:
        issues.append(OrphanIssue(
            kind="option_without_spot",
            details=f"Underlying {sym} has options but no spot/index bars",
        ))

    # Selections referencing instruments with no bars
    rows2 = conn.execute(
        """
        SELECT COUNT(*) FROM option_selection os
        WHERE os.instrument_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohlcv o WHERE o.instrument_id = os.instrument_id
          )
        """
    ).fetchone()
    if rows2 and rows2[0] > 0:
        issues.append(OrphanIssue(
            kind="selection_no_bars",
            details=f"{rows2[0]} option_selection rows reference instruments with no OHLCV bars",
        ))

    return issues


# ---------------------------------------------------------------------------
# Check 5: Feature coverage
# ---------------------------------------------------------------------------

def check_feature_coverage(conn: Any) -> list[FeatureCoverageIssue]:
    """Instruments with bars but no computed VWAP in feature table."""
    issues: list[FeatureCoverageIssue] = []

    rows = conn.execute(
        """
        SELECT
            o.instrument_id,
            i.raw_ticker,
            COUNT(*) AS bars,
            COUNT(f.ts) AS vwap_rows
        FROM ohlcv o
        JOIN instrument i ON o.instrument_id = i.instrument_id
        LEFT JOIN feature f ON f.instrument_id = o.instrument_id AND f.ts = o.ts
        GROUP BY o.instrument_id, i.raw_ticker
        HAVING COUNT(f.ts) < COUNT(*)
        ORDER BY o.instrument_id
        """
    ).fetchall()

    for inst_id, raw_ticker, bars, vwap_rows in rows:
        issues.append(FeatureCoverageIssue(
            instrument_id=inst_id,
            raw_ticker=raw_ticker or str(inst_id),
            bars_count=bars,
            vwap_count=vwap_rows,
        ))

    return issues


# ---------------------------------------------------------------------------
# Master runner
# ---------------------------------------------------------------------------

def run_all_checks() -> AuditReport:
    """Run all audit checks and return a consolidated AuditReport."""
    settings = get_settings()
    report = AuditReport()

    with get_connection() as conn:
        logger.info("audit_check_1_coverage")
        report.strike_issues = check_instrument_coverage(conn)

        logger.info("audit_check_2_gaps")
        report.gap_issues = check_time_series_gaps(conn)

        logger.info("audit_check_3_oi")
        report.oi_issues = check_missing_open_interest(conn)

        logger.info("audit_check_4_orphans")
        report.orphan_issues = check_orphans(conn)

        logger.info("audit_check_5_features")
        report.feature_issues = check_feature_coverage(conn)

        # Build per-underlying summary
        underlyings = conn.execute(
            "SELECT underlying_id, symbol FROM underlying ORDER BY symbol"
        ).fetchall()

        for und_id, symbol in underlyings:
            strike_issues = [i for i in report.strike_issues if i.underlying_symbol == symbol]
            pass_fail = "PASS"
            if strike_issues:
                pass_fail = "FAIL"
            report.underlying_results[symbol] = {
                "pass_fail": pass_fail,
                "strike_issues": len(strike_issues),
                "gap_issues": sum(
                    1 for g in report.gap_issues
                    if _instrument_belongs_to_underlying(conn, g.instrument_id, und_id)
                ),
            }

    # Critical failure determination
    if report.strike_issues:
        report.critical_failures.append(
            f"{len(report.strike_issues)} underlying(s) have missing strikes or CE/PE pairs"
        )

    critical_gaps = [
        g for g in report.gap_issues
        if g.gap_pct > settings.audit.max_gap_pct
    ]
    if critical_gaps:
        report.critical_failures.append(
            f"{len(critical_gaps)} instrument(s) have gap% > {settings.audit.max_gap_pct}"
        )

    if settings.audit.missing_oi_critical and report.oi_issues:
        report.critical_failures.append(
            f"{len(report.oi_issues)} instrument(s) have missing open interest"
        )

    if report.orphan_issues:
        report.critical_failures.append(
            f"{len(report.orphan_issues)} orphan/integrity issue(s) found"
        )

    logger.info(
        "audit_complete",
        strike_issues=len(report.strike_issues),
        gap_issues=len(report.gap_issues),
        oi_issues=len(report.oi_issues),
        orphan_issues=len(report.orphan_issues),
        feature_issues=len(report.feature_issues),
        critical=len(report.critical_failures),
    )
    return report


def _instrument_belongs_to_underlying(conn: Any, instrument_id: int, underlying_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM instrument WHERE instrument_id = %s AND underlying_id = %s",
        (instrument_id, underlying_id),
    ).fetchone()
    return row is not None
