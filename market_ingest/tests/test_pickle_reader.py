from __future__ import annotations

from datetime import datetime, timezone
import zoneinfo

import pandas as pd

from app.config import ColumnMapProfile
from app.ingest.pipeline import _parse_timestamp
from app.ingest.reader import read_file_batches


def test_pickle_dataframe_is_read_as_batches(tmp_path):
    path = tmp_path / "sample.pkl"
    frame = pd.DataFrame(
        [
            {
                "ticker": "NIFTY240123CE",
                "date": "2024-01-01",
                "time": "09:15:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1200,
                "open_interest": 345,
            },
            {
                "ticker": "NIFTY240123CE",
                "date": "2024-01-01",
                "time": "09:16:00",
                "open": 100.5,
                "high": 101.5,
                "low": 100.0,
                "close": 101.0,
                "volume": 1300,
                "open_interest": 350,
            },
        ]
    )
    frame.to_pickle(path)

    batches = list(read_file_batches(path, batch_size=1))

    assert len(batches) == 2
    assert batches[0].shape[0] == 1
    assert batches[0].columns == [
        "ticker",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]


def test_parse_timestamp_accepts_combined_datetime_value():
    profile = ColumnMapProfile(
        granularity="intraday",
        timezone="Asia/Kolkata",
        datetime_format="%m/%d/%Y",
    )
    ts = _parse_timestamp(
        "2024-01-01 09:15:59",
        None,
        profile,
        zoneinfo.ZoneInfo("Asia/Kolkata"),
    )

    assert ts.year == 2024
    assert ts.month == 1
    assert ts.day == 1
    assert ts.hour == 9
    assert ts.minute == 15
    assert ts.tzinfo is not None
