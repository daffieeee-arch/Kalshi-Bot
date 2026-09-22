"""Parse CF Benchmarks BRTI frames and the quarter-hour final minute."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

_QUARTER_MS = 15 * 60 * 1000
_FINAL_MINUTE_MS = 60 * 1000


def in_final_minute(ts_ms: int) -> bool:
    """True when `ts_ms` is inside `(quarter_close - 60s, quarter_close]`.

    Quarter closes are UTC :00, :15, :30 and :45. The start boundary is excluded
    and the close tick is included, matching Kalshi's
    `last_60s_windowed_average_15min` window.
    """
    if ts_ms % _QUARTER_MS == 0:
        close = ts_ms
    else:
        close = ts_ms - (ts_ms % _QUARTER_MS) + _QUARTER_MS
    return (close - _FINAL_MINUTE_MS) < ts_ms <= close


def quarter_marks_ms(day: datetime) -> list[int]:
    """UTC quarter-hour closes for one calendar day, as unix milliseconds."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    return [base + i * _QUARTER_MS for i in range(24 * 4)]


def _embedded_frame(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _window(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {
            "value": None,
            "window_size": None,
            "window_start_ts_ms": None,
            "window_end_ts_exclusive": None,
        }
    return {
        "value": block.get("value"),
        "window_size": block.get("window_size"),
        "window_start_ts_ms": block.get("window_start_ts_ms"),
        "window_end_ts_exclusive": block.get("window_end_ts_exclusive"),
    }


def parse_brti(envelope: dict[str, Any]) -> dict[str, Any]:
    """Normalize a 1 Hz `cfbenchmarks_value` message. Missing averages stay None."""
    msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
    frame = _embedded_frame(msg.get("data"))
    avg = _window(msg.get("avg_60s_data"))
    final_raw = msg.get("last_60s_windowed_average_15min")
    final = _window(final_raw) if isinstance(final_raw, dict) else _window(None)
    return {
        "index_id": msg.get("index_id"),
        "source_ts": frame.get("time"),
        "provider_received_at": msg.get("received_at"),
        "value": frame.get("value"),
        "raw_data": msg.get("data"),
        "avg_60s_value": avg["value"],
        "avg_60s_window_size": avg["window_size"],
        "avg_60s_window_start_ts_ms": avg["window_start_ts_ms"],
        "avg_60s_window_end_ts_exclusive": avg["window_end_ts_exclusive"],
        "final_minute_value": final["value"],
        "final_minute_window_size": final["window_size"],
        "final_minute_window_start_ts_ms": final["window_start_ts_ms"],
        "final_minute_window_end_ts_exclusive": final["window_end_ts_exclusive"],
    }


def parse_brti_5hz(envelope: dict[str, Any]) -> dict[str, Any]:
    """Normalize a 5 Hz tick. This channel does not carry 60-second averages."""
    msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
    return {
        "index_id": msg.get("index_id"),
        "source_ts": msg.get("source_ts_ms"),
        "provider_received_at": msg.get("received_at"),
        "value": msg.get("value_usd"),
        "raw_data": msg.get("data"),
        "avg_60s_value": None,
        "avg_60s_window_size": None,
        "avg_60s_window_start_ts_ms": None,
        "avg_60s_window_end_ts_exclusive": None,
        "final_minute_value": None,
        "final_minute_window_size": None,
        "final_minute_window_start_ts_ms": None,
        "final_minute_window_end_ts_exclusive": None,
    }
