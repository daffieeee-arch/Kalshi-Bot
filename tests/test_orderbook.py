"""Orderbook snapshot / delta / gap rules from docs.kalshi.com/websockets/orderbook-updates."""

from __future__ import annotations

from decimal import Decimal

from kalshi_bot.orderbook import OrderbookState


def _snapshot(**extra):
    msg = {
        "market_ticker": "KXBTC15M-TEST",
        "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "yes_dollars_fp": [["0.4000", "10.00"]],
        "no_dollars_fp": [["0.5200", "4.00"]],
    }
    msg.update(extra)
    return {"type": "orderbook_snapshot", "sid": 2, "seq": 2, "msg": msg}


def _delta(seq: int, **extra):
    msg = {
        "market_ticker": "KXBTC15M-TEST",
        "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "price_dollars": "0.4000",
        "delta_fp": "2.00",
        "side": "yes",
        "ts_ms": 1669149841000,
    }
    msg.update(extra)
    return {"type": "orderbook_delta", "sid": 2, "seq": seq, "msg": msg}


def test_snapshot_then_delta_adds() -> None:
    book = OrderbookState()
    assert book.apply_snapshot(_snapshot()).ok
    assert book.valid
    assert book.best_bid("yes") == Decimal("0.4000")
    assert book.implied_ask("yes") == Decimal("0.4800")
    assert book.apply_delta(_delta(3)).ok
    assert book.size_at("yes", Decimal("0.4000")) == Decimal("12.00")


def test_zero_qty_removes_level() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    result = book.apply_delta(_delta(3, delta_fp="-10.00"))
    assert result.ok
    assert book.size_at("yes", Decimal("0.4000")) == Decimal("0")
    assert Decimal("0.4000") not in book.yes


def test_negative_qty_invalidates() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    result = book.apply_delta(_delta(3, delta_fp="-11.00"))
    assert not result.ok
    assert result.need_snapshot
    assert book.valid is False


def test_seq_gap_invalidates() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    result = book.apply_delta(_delta(5))
    assert not result.ok
    assert result.invalid_reason == "seq_gap"
    assert book.valid is False


def test_delta_before_snapshot_asks_for_snapshot() -> None:
    book = OrderbookState()
    result = book.apply_delta(_delta(1))
    assert result.need_snapshot
    assert book.valid is False
