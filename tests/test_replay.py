"""Last-minute replay uses the live hint and official settlement."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshi_bot.replay import MarketInfo, collect_tickers, replay
from kalshi_bot.signal import evaluate, realized_sigma
from test_orderbook import _snapshot


_NY = ZoneInfo("America/New_York")
_TICKER = "KXBTC15M-26SEP221015-15"


def _ts(hour: int, minute: int, second: int) -> int:
    when = datetime(2026, 9, 22, hour, minute, second, tzinfo=_NY)
    return int(when.timestamp() * 1000)


def _row(stream: str, payload: dict, ts_ms: int) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "source_env": "production",
            "stream": stream,
            "ts_ms": ts_ms,
            "local_received_at": "2026-09-22T14:14:00+00:00",
            "payload": payload,
        }
    )


def _brti(value: str, close_avg: str, n: int, ts_ms: int) -> dict:
    return {
        "type": "cfbenchmarks_value",
        "sid": 1,
        "seq": n,
        "msg": {
            "index_id": "BRTI",
            "received_at": ts_ms,
            "data": json.dumps({"type": "value", "time": ts_ms, "id": "BRTI", "value": value}),
            "last_60s_windowed_average_15min": {"value": close_avg, "window_size": n},
        },
    }


def _write(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_collect_tickers_skips_deltas(tmp_path: Path) -> None:
    ts = _ts(10, 14, 10)
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("trade", {"msg": {"market_ticker": "SKIP"}}, ts),
            _row("orderbook_snapshot", _snapshot(market_ticker=_TICKER), ts),
            _row("market_meta", {"ticker": "KXBTC15M-NEXT", "floor_strike": "1"}, ts),
        ],
    )
    assert collect_tickers(jsonl) == [_TICKER, "KXBTC15M-NEXT"]


def test_replay_takes_last_minute_yes_and_settles(tmp_path: Path) -> None:
    ts_open = _ts(10, 14, 10)
    ts_close = _ts(10, 15, 0)
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("orderbook_snapshot", _snapshot(market_ticker=_TICKER), ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85900", 10, ts_open), ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85950", 60, ts_close), ts_close),
        ],
    )
    markets = {
        _TICKER: MarketInfo(_TICKER, Decimal("85000"), "yes", "finalized"),
    }
    report = replay(jsonl, markets)
    assert len(report.traded) == 1
    row = report.traded[0]
    assert row.side == "yes"
    assert row.hint == "paper YES?"
    assert row.yes_won is True
    assert row.pnl is not None and row.pnl > 0
    assert report.wins == 1


def test_replay_waits_when_book_already_locked(tmp_path: Path) -> None:
    ts_open = _ts(10, 14, 10)
    ts_close = _ts(10, 15, 0)
    snap = _snapshot(market_ticker=_TICKER, yes_dollars_fp=[["0.0100", "10.00"]], no_dollars_fp=[["0.0100", "10.00"]])
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("orderbook_snapshot", snap, ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85900", 10, ts_open), ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85950", 60, ts_close), ts_close),
        ],
    )
    markets = {
        _TICKER: MarketInfo(_TICKER, Decimal("85000"), "yes", "finalized"),
    }
    locked = evaluate(
        spot=Decimal("86000"),
        strike=Decimal("85000"),
        seconds_left=50,
        close_avg=Decimal("85900"),
        close_window=10,
        yes_ask=Decimal("0.99"),
        no_ask=Decimal("0.99"),
        sigma=realized_sigma([]),
    )
    assert locked.hint == "wait"
    report = replay(jsonl, markets)
    assert report.traded == []
    assert report.waits == 1
    assert report.windows[0].official_result == "yes"


def test_replay_ignores_midwindow_hint(tmp_path: Path) -> None:
    ts_mid = _ts(10, 2, 0)
    ts_close = _ts(10, 15, 0)
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("orderbook_snapshot", _snapshot(market_ticker=_TICKER), ts_mid),
            _row("cfbenchmarks_value", _brti("86000", "85900", 0, ts_mid), ts_mid),
            _row("cfbenchmarks_value", _brti("86000", "85950", 60, ts_close), ts_close),
        ],
    )
    markets = {
        _TICKER: MarketInfo(_TICKER, Decimal("85000"), "yes", "finalized"),
    }
    mid = evaluate(
        spot=Decimal("86000"),
        strike=Decimal("85000"),
        seconds_left=780,
        close_avg=None,
        close_window=None,
        yes_ask=Decimal("0.48"),
        no_ask=Decimal("0.60"),
        sigma=realized_sigma([]),
    )
    assert mid.regime == "mid"
    report = replay(jsonl, markets)
    assert report.traded == []


def test_replay_reads_strike_from_market_meta(tmp_path: Path) -> None:
    ts_open = _ts(10, 14, 10)
    ts_close = _ts(10, 15, 0)
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("market_meta", {"ticker": _TICKER, "floor_strike": "85000"}, ts_open),
            _row("orderbook_snapshot", _snapshot(market_ticker=_TICKER), ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85900", 10, ts_open), ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85950", 60, ts_close), ts_close),
        ],
    )
    report = replay(jsonl, {})
    assert report.traded[0].strike == Decimal("85000")
    assert report.traded[0].yes_won is True
