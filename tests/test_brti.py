"""BRTI parsing and quarter-hour final-minute boundaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.brti import in_final_minute, parse_brti, parse_brti_5hz, quarter_marks_ms

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_brti_keeps_average_and_raw_frame() -> None:
    envelope = json.loads((FIXTURES / "brti_value.json").read_text())
    parsed = parse_brti(envelope)
    assert parsed["index_id"] == "BRTI"
    assert parsed["value"] == "68000.12"
    assert parsed["source_ts"] == 1710000000123
    assert parsed["provider_received_at"] == 1710000000123
    assert parsed["avg_60s_value"] == "68000.12000000"
    assert parsed["avg_60s_window_size"] == 3
    assert parsed["final_minute_value"] is None
    assert "68000.12" in parsed["raw_data"]


def test_final_minute_field_only_when_present() -> None:
    envelope = json.loads((FIXTURES / "brti_value.json").read_text())
    envelope["msg"]["last_60s_windowed_average_15min"] = {
        "value": "68000.23000000",
        "window_size": 60,
        "window_start_ts_ms": 1709999940000,
        "window_end_ts_exclusive": 1710000000000,
    }
    parsed = parse_brti(envelope)
    assert parsed["final_minute_value"] == "68000.23000000"
    assert parsed["final_minute_window_size"] == 60


def test_5hz_has_no_averages() -> None:
    parsed = parse_brti_5hz(
        {
            "type": "cfbenchmarks_value_5hz",
            "sid": 1,
            "seq": 1,
            "msg": {
                "index_id": "BRTI",
                "value_usd": "68000.12000000",
                "source_ts_ms": 1710000000323,
                "received_at": 1710000000341,
                "data": "{\"value\":\"68000.12\"}",
            },
        }
    )
    assert parsed["value"] == "68000.12000000"
    assert parsed["source_ts"] == 1710000000323
    assert parsed["avg_60s_value"] is None
    assert parsed["final_minute_value"] is None


def test_quarter_boundaries_include_close_and_exclude_the_next_second() -> None:
    day = datetime(2026, 9, 21, tzinfo=timezone.utc)
    marks = [mark for mark in quarter_marks_ms(day) if mark % (15 * 60 * 1000) == 0]
    assert len(marks) == 96
    for close in marks:
        assert in_final_minute(close)
        assert in_final_minute(close - 1000)
        assert not in_final_minute(close + 1000)
        assert not in_final_minute(close - 60_000)
        assert in_final_minute(close - 60_000 + 1)
