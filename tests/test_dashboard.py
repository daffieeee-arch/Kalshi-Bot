"""Dashboard feed rebuilds the book from the capture log, not from REST."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshi_bot.dashboard.feed import LiveFeed, parse_kxbtc15m_close
from test_orderbook import _delta, _snapshot


def _row(stream: str, payload: dict) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "source_env": "production",
            "stream": stream,
            "ts_ms": 1,
            "local_received_at": "2026-09-22T14:00:00+00:00",
            "payload": payload,
        }
    )


def test_parse_kxbtc15m_close_is_new_york() -> None:
    close = parse_kxbtc15m_close("KXBTC15M-26SEP221015-15")
    assert close == datetime(2026, 9, 22, 10, 15, tzinfo=ZoneInfo("America/New_York"))
    assert parse_kxbtc15m_close("OTHER") is None


def test_feed_replays_snapshot_then_delta(tmp_path: Path) -> None:
    jsonl = tmp_path / "cap.jsonl"
    jsonl.write_text(
        "\n".join(
            [
                _row("trade", {"type": "trade", "sid": 3, "seq": 1, "msg": {"market_ticker": "OLD"}}),
                _row("orderbook_snapshot", _snapshot()),
                _row("orderbook_delta", _delta(3)),
                "",
            ]
        ),
        encoding="utf-8",
    )
    lock = tmp_path / "recorder.lock"
    lock.write_text("pid=1\n", encoding="utf-8")
    feed = LiveFeed(jsonl_path=jsonl, lock_path=lock)
    feed.start()
    try:
        deadline = time.time() + 2
        snap = feed.snapshot()
        while time.time() < deadline and snap["book_status"] != "live":
            time.sleep(0.05)
            snap = feed.snapshot()
        assert snap["book_status"] == "live"
        assert snap["ticker"] == "KXBTC15M-TEST"
        assert snap["book"]["yes_bid"] == "0.4000"
        assert any(row[0] == "0.4000" and row[1] == "12.00" for row in snap["book"]["yes"])
    finally:
        feed.stop()
