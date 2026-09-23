"""Counterexamples for settlement, fills, book identity, and entry hints.

These pin the R1–R6 failures from the 2026-09-22 review. They describe the
intended behavior; they do not copy an external reference implementation.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

import kalshi_bot.dashboard.feed as feed_mod
from kalshi_bot.client import KalshiDemoClient
from kalshi_bot.config import DEMO_REST_BASE, DEMO_WS_URL, PROD_REST_BASE, PROD_WS_URL, Settings
from kalshi_bot.dashboard.feed import LiveFeed
from kalshi_bot.discover import parse_kxbtc15m_close
from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.paper import PaperIntent, PaperLedger, PaperStyle
from kalshi_bot.replay import MarketInfo, replay
from kalshi_bot.signal import evaluate, last_minute_yes_prob, realized_sigma
from test_orderbook import _delta, _snapshot
from test_replay import _TICKER, _brti, _row, _ts, _write


def _yes_intent(count: str, *, price: str = "0.40", style: PaperStyle = "taker") -> PaperIntent:
    return PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal(price),
        count=Decimal(count),
        style=style,
    )


def _book_ask(size: str) -> OrderbookState:
    """YES implied ask 0.40 with `size` contracts on the NO bid."""
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.1000", "5.00"]],
            no_dollars_fp=[["0.6000", size]],
        )
    )
    return book


def _trade(count: str, *, yes_price: str = "0.3900") -> dict:
    return {
        "type": "trade",
        "sid": 3,
        "seq": 2,
        "msg": {
            "trade_id": "d91bc706-ee49-470d-82d8-11418bda6fee",
            "market_ticker": "KXBTC15M-TEST",
            "yes_price_dollars": yes_price,
            "no_price_dollars": "0.6100",
            "count_fp": count,
            "taker_side": "no",
            "taker_outcome_side": "no",
            "taker_book_side": "ask",
            "ts_ms": 2000,
        },
    }


def test_official_settlement_drives_pnl_when_reconstruction_disagrees() -> None:
    """R1: official NO must book a loss even if close_avg >= strike."""
    ledger = PaperLedger()
    intent = _yes_intent("1")
    ledger.add(intent)
    fills = ledger.on_book(_book_ask("10.00"), ts_ms=1)
    assert len(fills) == 1
    assert fills[0].price == Decimal("0.40")
    mark = ledger.on_settlement(
        market_ticker="KXBTC15M-TEST",
        close_avg=Decimal("85000.01"),
        floor_strike=Decimal("85000"),
        official_result="no",
    )
    fee = quadratic_taker_fee(Decimal("1"), Decimal("0.40"))
    assert fee == Decimal("0.016800")
    assert mark is not None
    assert mark.yes_won is False
    assert mark.source == "official"
    assert mark.reconstructed_yes is True
    assert mark.mismatch is True
    assert intent.won is False
    assert intent.pnl == Decimal("-0.416800")
    assert intent.pnl == -Decimal("0.40") - fee


def test_replay_pnl_follows_official_result_and_reports_mismatch(tmp_path: Path) -> None:
    ts_open = _ts(10, 14, 10)
    ts_close = _ts(10, 15, 0)
    snap = _snapshot(
        market_ticker=_TICKER,
        yes_dollars_fp=[["0.1000", "5.00"]],
        no_dollars_fp=[["0.6000", "10.00"]],
    )
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("orderbook_snapshot", snap, ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85900", 15, ts_open), ts_open),
            _row("cfbenchmarks_value", _brti("86000", "86000", 60, ts_close), ts_close),
        ],
    )
    markets = {_TICKER: MarketInfo(_TICKER, Decimal("85000"), "no", "finalized")}
    row = replay(jsonl, markets).traded[0]
    assert row.side == "yes"
    assert row.yes_won is False
    assert row.reconstructed_yes is True
    assert row.outcome_mismatch is True
    assert row.pnl == Decimal("-0.416800")


def test_replay_ignores_late_close_print_from_another_quarter(tmp_path: Path) -> None:
    """A window_size of 60 inside the 90s grace is not this ticker's settlement."""
    ts_open = _ts(10, 14, 10)
    ts_late = _ts(10, 15, 45)
    snap = _snapshot(
        market_ticker=_TICKER,
        yes_dollars_fp=[["0.1000", "5.00"]],
        no_dollars_fp=[["0.6000", "10.00"]],
    )
    late = _brti("86000", "1000", 60, ts_late)
    late["msg"]["last_60s_windowed_average_15min"]["window_start_ts_ms"] = 1
    late["msg"]["last_60s_windowed_average_15min"]["window_end_ts_exclusive"] = 2
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("orderbook_snapshot", snap, ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85900", 15, ts_open), ts_open),
            _row("cfbenchmarks_value", late, ts_late),
        ],
    )
    markets = {_TICKER: MarketInfo(_TICKER, Decimal("85000"), "yes", "finalized")}
    report = replay(jsonl, markets)
    assert report.traded == []
    assert report.windows[0].skip == "open"
    assert report.windows[0].pnl is None
    assert report.windows[0].close_avg == Decimal("85900")


def test_replay_settles_a_late_print_when_the_window_belongs_to_the_ticker(tmp_path: Path) -> None:
    """The 90s grace still accepts the right quarter, identified by window bounds."""
    ts_open = _ts(10, 14, 10)
    ts_late = _ts(10, 15, 45)
    close = parse_kxbtc15m_close(_TICKER)
    assert close is not None
    close_ms = int(close.timestamp() * 1000)
    snap = _snapshot(
        market_ticker=_TICKER,
        yes_dollars_fp=[["0.1000", "5.00"]],
        no_dollars_fp=[["0.6000", "10.00"]],
    )
    late = _brti("86000", "86000", 60, ts_late)
    window = late["msg"]["last_60s_windowed_average_15min"]
    window["window_start_ts_ms"] = close_ms - 60_000
    window["window_end_ts_exclusive"] = close_ms
    jsonl = tmp_path / "cap.jsonl"
    _write(
        jsonl,
        [
            _row("orderbook_snapshot", snap, ts_open),
            _row("cfbenchmarks_value", _brti("86000", "85900", 15, ts_open), ts_open),
            _row("cfbenchmarks_value", late, ts_late),
        ],
    )
    markets = {_TICKER: MarketInfo(_TICKER, Decimal("85000"), "yes", "finalized")}
    row = replay(jsonl, markets).traded[0]
    assert row.yes_won is True
    assert row.outcome_mismatch is False
    assert row.pnl is not None and row.pnl > 0


def test_one_book_level_cannot_fill_two_intents() -> None:
    book = _book_ask("10.00")
    ledger = PaperLedger()
    first = _yes_intent("10")
    second = _yes_intent("10")
    ledger.add(first)
    ledger.add(second)
    fills = ledger.on_book(book, ts_ms=1)
    assert sum((fill.count for fill in fills), Decimal("0")) == Decimal("10")
    assert first.remaining == Decimal("0")
    assert second.remaining == Decimal("10")
    assert book.size_at("no", Decimal("0.6000")) == Decimal("10.00")


def test_unrelated_delta_does_not_refill_consumed_ask() -> None:
    book = _book_ask("10.00")
    ledger = PaperLedger()
    intent = _yes_intent("20")
    ledger.add(intent)
    first = ledger.on_book(book, ts_ms=1)
    assert sum((fill.count for fill in first), Decimal("0")) == Decimal("10")
    assert book.apply_delta(_delta(3, price_dollars="0.1000", delta_fp="1.00", side="yes")).ok
    second = ledger.on_book(book, ts_ms=2)
    assert second == []
    assert intent.remaining == Decimal("10")
    assert book.apply_delta(_delta(4, price_dollars="0.6000", delta_fp="5.00", side="no")).ok
    third = ledger.on_book(book, ts_ms=3)
    assert sum((fill.count for fill in third), Decimal("0")) == Decimal("5")
    assert intent.remaining == Decimal("5")


def test_one_trade_print_is_shared_across_maker_intents() -> None:
    ledger = PaperLedger()
    first = _yes_intent("10", style="maker")
    second = _yes_intent("10", style="maker")
    ledger.add(first)
    ledger.add(second)
    fills = ledger.on_trade(_trade("10.00"))
    assert sum((fill.count for fill in fills), Decimal("0")) == Decimal("10")
    assert first.remaining == Decimal("0")
    assert second.remaining == Decimal("10")


def test_no_fills_after_settlement_even_when_new_size_appears() -> None:
    book = _book_ask("10.00")
    ledger = PaperLedger()
    intent = _yes_intent("20")
    ledger.add(intent)
    ledger.on_book(book, ts_ms=1)
    ledger.on_settlement(
        market_ticker="KXBTC15M-TEST",
        close_avg=Decimal("86000"),
        floor_strike=Decimal("85000"),
        official_result="yes",
    )
    pnl = intent.pnl
    assert intent.status == "settled"
    assert intent.remaining == Decimal("0")
    assert book.apply_delta(_delta(3, price_dollars="0.6000", delta_fp="10.00", side="no")).ok
    assert ledger.on_book(book, ts_ms=2) == []
    assert ledger.on_trade(_trade("10.00")) == []
    assert intent.fills[-1].count == Decimal("10")
    assert intent.pnl == pnl
    assert len(intent.fills) == 1


def test_delta_for_other_ticker_does_not_mutate_book() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    foreign = _delta(3, market_ticker="KXBTC15M-OTHER", delta_fp="50.00")
    result = book.apply_delta(foreign)
    assert result.ok is False
    assert result.need_snapshot is True
    assert result.invalid_reason == "ticker_mismatch"
    assert book.valid is False
    assert book.market_ticker == "KXBTC15M-TEST"
    assert book.last_seq == 2
    assert book.best_bid("yes") is None


def test_incomplete_snapshot_invalidates_previous_book() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    result = book.apply_snapshot({"type": "orderbook_snapshot", "sid": 2, "seq": 9, "msg": {}})
    assert result.ok is False
    assert result.invalid_reason == "snapshot_missing_fields"
    assert book.valid is False
    assert book.market_ticker == "KXBTC15M-TEST"
    assert book.best_bid("yes") is None
    assert book.implied_ask("yes") is None


def test_last_minute_average_uses_random_walk_mean_variance() -> None:
    """SD(mean of next r levels) = sigma * sqrt((r+1)*(2r+1)/(6r)), not sigma*sqrt(r)."""
    remaining = 30
    scale = math.sqrt((remaining + 1) * (2 * remaining + 1) / (6 * remaining))
    z = 20.0 / (6.7 * scale)
    expected = Decimal(str(0.5 * math.erfc(-z / math.sqrt(2.0))))
    got = last_minute_yes_prob(
        Decimal("85000"),
        30,
        Decimal("85020"),
        Decimal("85000"),
        Decimal("6.7"),
    )
    assert abs(got - expected) < Decimal("0.0001")
    assert Decimal("0.81") < got < Decimal("0.83")


def test_realized_sigma_scales_irregular_intervals() -> None:
    spots = [Decimal(0), Decimal(10), Decimal(0), Decimal(10), Decimal(0), Decimal(10), Decimal(0), Decimal(10), Decimal(0)]
    stamps = [0, 1000, 101_000, 102_000, 202_000, 203_000, 303_000, 304_000, 404_000]
    untimed = realized_sigma(spots)
    timed = realized_sigma(spots, timestamps_ms=stamps)
    assert timed < untimed
    assert timed != untimed


def test_evaluate_does_not_hint_on_closed_or_ineligible_market() -> None:
    juicy: dict[str, Any] = dict(
        spot=Decimal("85020"),
        strike=Decimal("85000"),
        close_avg=Decimal("85010"),
        close_window=30,
        yes_ask=Decimal("0.40"),
        no_ask=Decimal("0.70"),
        sigma=Decimal("6.7"),
    )
    closed = evaluate(seconds_left=-10, **juicy)
    assert closed.hint == "wait"
    assert closed.note == "market closed"
    invalid = evaluate(seconds_left=20, book_valid=False, **juicy)
    assert invalid.hint == "wait"
    assert invalid.note == "book invalid"
    stale = evaluate(seconds_left=20, data_fresh=False, **juicy)
    assert stale.hint == "wait"
    assert stale.note == "stale data"
    other = evaluate(
        seconds_left=20,
        book_ticker="KXBTC15M-A",
        reference_ticker="KXBTC15M-B",
        **juicy,
    )
    assert other.hint == "wait"
    assert other.note == "ticker mismatch"
    finalized = evaluate(seconds_left=20, market_status="finalized", **juicy)
    assert finalized.hint == "wait"
    assert finalized.note == "market not open"


def test_dashboard_snapshot_omits_entry_hint_when_book_invalid(tmp_path: Path) -> None:
    jsonl = tmp_path / "cap.jsonl"
    jsonl.write_text("", encoding="utf-8")
    feed = LiveFeed(jsonl_path=jsonl, lock_path=tmp_path / "recorder.lock")
    ticker = "KXBTC15M-26SEP221015-15"
    feed.book.apply_snapshot(
        _snapshot(
            market_ticker=ticker,
            yes_dollars_fp=[["0.1000", "5.00"]],
            no_dollars_fp=[["0.6000", "10.00"]],
        )
    )
    feed._market = {"ticker": ticker, "floor_strike": "85000", "status": "open", "title": "btc"}
    feed._brti = {"value": "86000", "close_avg": "85900", "close_window": 20}
    now = 1_000_000.0
    feed._book_mono = now
    feed._brti_mono = now
    feed._spots.append((1, Decimal("86000")))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return datetime(2026, 9, 22, 14, 14, 10, tzinfo=tz)

    original_datetime = feed_mod.datetime
    original_monotonic = feed_mod.time.monotonic
    feed_mod.datetime = _Clock
    feed_mod.time.monotonic = lambda: now
    try:
        live = feed.snapshot()
        assert live["signal"]["hint"] == "paper YES?"
        feed.book.valid = False
        blocked = feed.snapshot()
    finally:
        feed_mod.datetime = original_datetime
        feed_mod.time.monotonic = original_monotonic
    assert blocked["book"]["valid"] is False
    assert blocked["book"]["yes_ask"] is None
    assert blocked["signal"]["hint"] == "wait"


def test_dashboard_drops_close_average_when_book_ticker_changes(tmp_path: Path) -> None:
    jsonl = tmp_path / "cap.jsonl"
    jsonl.write_text("", encoding="utf-8")
    feed = LiveFeed(jsonl_path=jsonl, lock_path=tmp_path / "recorder.lock")
    first = "KXBTC15M-26SEP221015-15"
    second = "KXBTC15M-26SEP221030-30"
    feed._apply_row(_row("orderbook_snapshot", _snapshot(market_ticker=first), 1))
    feed._brti = {"value": "86000", "close_avg": "111", "close_window": 40}
    feed._apply_row(_row("orderbook_snapshot", _snapshot(market_ticker=second), 2))
    assert feed.book.market_ticker == second
    assert feed._brti.get("close_avg") is None
    assert feed._brti.get("close_window") is None
    assert feed._brti.get("value") == "86000"


def test_demo_client_rejects_absolute_url_before_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        env="demo",
        trade_env="paper",
        data_env="demo",
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        prod_rest_base=PROD_REST_BASE,
        prod_ws_url=PROD_WS_URL,
        api_key_id=None,
        private_key_path=None,
        prod_api_key_id=None,
        prod_private_key_path=None,
    )
    with KalshiDemoClient(settings) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url=DEMO_REST_BASE + "/",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(ValueError, match="absolute"):
            client.get("https://external-api.kalshi.com/trade-api/v2/markets")
        assert seen == []
        body = client.get("/markets")
        assert body == {"ok": True}
        assert seen == [f"{DEMO_REST_BASE}/markets"]
