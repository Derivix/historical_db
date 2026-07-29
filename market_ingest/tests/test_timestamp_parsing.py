from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import ColumnMapProfile
from app.ingest.pipeline import _parse_timestamp


def test_parse_timestamp_accepts_timezone_offset_in_time_value() -> None:
    profile = ColumnMapProfile(
        granularity="intraday",
        timezone="Asia/Kolkata",
        datetime_format="%d-%m-%Y",
        column_map={},
    )

    ts = _parse_timestamp(
        "01-01-2026",
        "09:15:00+05:30",
        profile,
        ZoneInfo("Asia/Kolkata"),
    )

    assert ts == datetime(2026, 1, 1, 3, 45, tzinfo=timezone.utc)
