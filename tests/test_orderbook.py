"""Order book snapshot, delta, gap, and reconnect behaviour."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kalshi_bot.orderbook import Orderbook, health_from_apply
from kalshi_bot.record import SegmentWriter

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_snapshot_and_delta_update_levels() -> None:
    book = Orderbook()
    snapshot = _load("orderbook_snapshot.json")
    assert book.apply_snapshot(snapshot).ok
    assert book.valid
    assert book.yes["0.2200"] == Decimal("10.00")
    assert book.no["0.5400"] == Decimal("20.00")

    delta = _load("orderbook_delta.json")
    assert book.apply_delta(delta).applied
    assert book.yes["0.2200"] == Decimal("15.00")
    assert book.seq == 3


def test_zero_delta_removes_level() -> None:
    book = Orderbook()
    book.apply_snapshot(_load("orderbook_snapshot.json"))
    delta = _load("orderbook_delta.json")
    delta["msg"]["delta_fp"] = "-10.00"
    assert book.apply_delta(delta).ok
    assert "0.2200" not in book.yes


def test_negative_level_invalidates_without_applying() -> None:
    book = Orderbook()
    book.apply_snapshot(_load("orderbook_snapshot.json"))
    delta = _load("orderbook_delta.json")
    delta["msg"]["delta_fp"] = "-11.00"
    result = book.apply_delta(delta)
    assert result.invalid_reason == "negative_level"
    assert result.need_snapshot
    assert not book.valid
    assert book.yes["0.2200"] == Decimal("10.00")
    health = health_from_apply(result, seq=3, market_ticker="KXBTC15M-TEST")
    assert health is not None
    assert health["health_reason"] == "negative_level"


def test_sequence_gap_does_not_invent_levels() -> None:
    book = Orderbook()
    book.apply_snapshot(_load("orderbook_snapshot.json"))
    before = deepcopy(book.yes)
    delta = _load("orderbook_delta.json")
    delta["seq"] = 5
    result = book.apply_delta(delta)
    assert result.invalid_reason == "sequence_gap"
    assert result.need_snapshot
    assert not book.valid
    assert book.yes == before


def test_delta_without_snapshot_requests_snapshot() -> None:
    book = Orderbook()
    result = book.apply_delta(_load("orderbook_delta.json"))
    assert result.invalid_reason == "missing_snapshot"
    assert book.yes == {}
    assert not book.valid


def test_reconnect_reset_requires_a_new_snapshot() -> None:
    book = Orderbook()
    book.apply_snapshot(_load("orderbook_snapshot.json"))
    book.reset()
    assert not book.valid
    assert book.yes == {}
    result = book.apply_delta(_load("orderbook_delta.json"))
    assert result.need_snapshot
    assert book.apply_snapshot(_load("orderbook_snapshot.json")).ok
    assert book.valid


def test_gap_health_event_is_written(tmp_path: Path) -> None:
    book = Orderbook()
    book.apply_snapshot(_load("orderbook_snapshot.json"))
    delta = _load("orderbook_delta.json")
    delta["seq"] = 9
    result = book.apply_delta(delta)
    health = health_from_apply(result, seq=9, market_ticker=book.market_ticker)
    assert health is not None
    now = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)
    writer = SegmentWriter(tmp_path)
    writer.write("health", received_at=now, payload=delta, normalized=health, now=now)
    writer.close()
    stored = next(tmp_path.rglob("stream=health/*.jsonl")).read_text()
    assert '"health_reason":"sequence_gap"' in stored
    assert '"source_env":"demo"' in stored
