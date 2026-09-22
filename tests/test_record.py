"""Raw JSONL rotation, Parquet compaction, and crash leftovers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from kalshi_bot.record import SCHEMA_VERSION, SegmentWriter, quarter_bucket


def _now() -> datetime:
    return datetime(2026, 9, 21, 23, 10, tzinfo=timezone.utc)


def test_closed_segment_is_jsonl_and_parquet(tmp_path: Path) -> None:
    writer = SegmentWriter(tmp_path)
    now = _now()
    writer.write(
        "orderbook",
        received_at=now,
        payload={"type": "orderbook_snapshot"},
        normalized={"market_ticker": "KXBTC15M-TEST", "book_valid": True},
        now=now,
    )
    writer.close()
    jsonl = next(tmp_path.rglob("stream=orderbook/*.jsonl"))
    parquet = jsonl.with_suffix(".parquet")
    assert jsonl.exists()
    assert parquet.exists()
    assert not list(tmp_path.rglob("*.partial"))
    row = json.loads(jsonl.read_text().strip())
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["source_env"] == "demo"
    assert row["market_ticker"] == "KXBTC15M-TEST"
    table = pq.read_table(parquet)
    assert table.num_rows == 1
    assert table.column("schema_version")[0].as_py() == 1
    assert "source_env=demo" in str(jsonl)
    assert "date=2026-09-21" in str(jsonl)


def test_partial_crash_leaves_previous_segment(tmp_path: Path) -> None:
    first = datetime(2026, 9, 21, 23, 1, tzinfo=timezone.utc)
    finished = SegmentWriter(tmp_path)
    finished.write(
        "health",
        received_at=first,
        payload={"type": "health", "n": 1},
        normalized={"health_reason": "sequence_gap"},
        now=first,
    )
    finished.close()
    closed = next(tmp_path.rglob("stream=health/*.jsonl"))
    closed_text = closed.read_text()

    second = datetime(2026, 9, 21, 23, 2, tzinfo=timezone.utc)
    crashed = SegmentWriter(tmp_path)
    crashed.write(
        "health",
        received_at=second,
        payload={"type": "health", "n": 2},
        normalized={"health_reason": "still_open"},
        now=second,
    )
    assert closed.read_text() == closed_text
    partials = list(tmp_path.rglob("*.jsonl.partial"))
    assert len(partials) == 1
    assert "still_open" in partials[0].read_text()
    assert "still_open" not in closed_text


def test_quarter_change_rotates_segment(tmp_path: Path) -> None:
    writer = SegmentWriter(tmp_path)
    first = datetime(2026, 9, 21, 23, 14, tzinfo=timezone.utc)
    second = datetime(2026, 9, 21, 23, 15, tzinfo=timezone.utc)
    assert quarter_bucket(first) != quarter_bucket(second)
    writer.write("brti", received_at=first, payload={"n": 1}, now=first)
    writer.write("brti", received_at=second, payload={"n": 2}, now=second)
    writer.close()
    segments = list(tmp_path.rglob("stream=brti/*.jsonl"))
    assert len(segments) == 2
