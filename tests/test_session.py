"""Fake bankroll, official settlement, persistence, and the session lock."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from kalshi_bot.account import PaperAccount
from kalshi_bot.cli import build_paper_parser
from kalshi_bot.dashboard.app import create_app
from kalshi_bot.dashboard.feed import LiveFeed
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.paper import PaperFill, PaperIntent
from kalshi_bot.session import (
    PaperSession,
    SessionLock,
    assert_paper_only,
    parse_official_result,
)
from kalshi_bot.strategy import StrategyParams
from test_orderbook import _snapshot

_TICKER = "KXBTC15M-26SEP221015-15"
_STARTED = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
_IN_WINDOW = datetime(2026, 9, 22, 14, 5, tzinfo=timezone.utc)
_AFTER_CLOSE = datetime(2026, 9, 22, 14, 20, tzinfo=timezone.utc)


def _session(tmp_path) -> PaperSession:
    return PaperSession.create(
        tmp_path / "sessions",
        bankroll=Decimal("1000"),
        hours=24,
        target_return=Decimal("0.50"),
        now=_STARTED,
    )


def _maker_trade(ticker: str = _TICKER) -> dict:
    return {
        "stream": "trade",
        "ts_ms": 2_000,
        "payload": {
            "type": "trade",
            "msg": {
                "market_ticker": ticker,
                "yes_price_dollars": "0.4000",
                "no_price_dollars": "0.6000",
                "count_fp": "2.00",
                "taker_book_side": "ask",
                "ts_ms": 2000,
            },
        },
    }


class _OfficialYes:
    def official_result(self, ticker: str) -> str:
        assert ticker == _TICKER
        return "yes"

    def floor_strike(self, ticker: str) -> Decimal:
        return Decimal("100")


def test_bankroll_debits_cost_and_credits_a_win() -> None:
    account = PaperAccount(Decimal("1000"))
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("2"),
        style="maker",
    )
    account.ledger.add(intent)
    account.note_fills(account.ledger.on_trade(_maker_trade("KXBTC15M-TEST")["payload"]))
    assert account.cash == Decimal("999.20")
    account.ledger.on_settlement(
        market_ticker="KXBTC15M-TEST",
        official_result="yes",
    )
    assert account.credit_settlements() == Decimal("2")
    assert account.cash == Decimal("1001.20")
    assert account.realized_pnl() == Decimal("1.20")
    assert account.equity(OrderbookState()) == Decimal("1001.20")
    assert account.unrealized_pnl(OrderbookState()) == Decimal("0")
    assert account.credit_settlements() == Decimal("0")


def test_official_loss_reduces_equity_by_cost_and_fee() -> None:
    account = PaperAccount(Decimal("1000"))
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.1000", "5.00"]],
            no_dollars_fp=[["0.6000", "10.00"]],
        )
    )
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("1"),
        style="taker",
    )
    account.ledger.add(intent)
    fills = account.ledger.on_book(book, ts_ms=1)
    account.note_fills(fills)
    account.ledger.on_settlement(
        market_ticker="KXBTC15M-TEST",
        close_avg=Decimal("85000.01"),
        floor_strike=Decimal("85000"),
        official_result="no",
    )
    account.credit_settlements()
    assert intent.pnl == Decimal("-0.416800")
    assert account.equity(OrderbookState()) == Decimal("1000") + intent.pnl


def test_session_settles_official_result_into_equity(tmp_path) -> None:
    session = _session(tmp_path)
    session.armed = True
    session.ticker = _TICKER
    intent = PaperIntent(
        market_ticker=_TICKER,
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("2"),
        style="maker",
    )
    session.account.ledger.add(intent)
    session.apply_live_row(_maker_trade(), now=_AFTER_CLOSE)
    session.lookup = _OfficialYes()
    session.poll_official(_AFTER_CLOSE)
    assert intent.won is True
    assert session.account.cash == Decimal("1001.20")
    assert session.account.equity(session.book) == Decimal("1001.20")
    session.save()
    loaded = PaperSession.load(session.root)
    assert loaded is not None
    assert loaded.account.cash == Decimal("1001.20")
    assert loaded.account.realized_pnl() == Decimal("1.20")
    assert loaded.account.equity(OrderbookState()) == Decimal("1001.20")
    assert loaded.account.credit_settlements() == Decimal("0")
    assert loaded.target_return == Decimal("0.50")
    assert "CEST" in loaded.view(_AFTER_CLOSE)["ends_at_amsterdam"]


def test_reload_does_not_take_the_same_displayed_size(tmp_path) -> None:
    session = _session(tmp_path)
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            market_ticker="KXBTC15M-TEST",
            no_dollars_fp=[["0.6000", "2.00"]],
        )
    )
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.99"),
        count=Decimal("10"),
        style="taker",
    )
    session.account.ledger.add(intent)
    fills = session.account.ledger.on_book(book, ts_ms=1)
    session.account.note_fills(fills)
    assert sum((fill.count for fill in fills), Decimal("0")) == Decimal("2")
    session.save()
    loaded = PaperSession.load(session.root)
    assert loaded is not None
    assert loaded.account.ledger.on_book(book, ts_ms=2) == []
    assert loaded.account.cash == session.account.cash


def test_session_adapts_after_four_losing_settlements(tmp_path) -> None:
    session = _session(tmp_path)
    session.started_at = _STARTED - timedelta(hours=12)
    for _ in range(4):
        intent = PaperIntent(
            market_ticker="KXBTC15M-TEST",
            outcome="yes",
            price=Decimal("0.40"),
            count=Decimal("1"),
            style="maker",
        )
        intent.remaining = Decimal("0")
        intent.status = "settled"
        intent.settled = True
        intent.won = False
        intent.pnl = Decimal("-0.40")
        intent.fills = [
            PaperFill(
                market_ticker="KXBTC15M-TEST",
                style="maker",
                outcome="yes",
                price=Decimal("0.40"),
                count=Decimal("1"),
                fee=Decimal("0"),
                ts_ms=1,
                source="trade",
            )
        ]
        session.account.ledger.add(intent)
    before = session.params.contracts
    session._maybe_adapt(_STARTED)
    assert session.params.contracts < before
    assert session.params.mid_edge > StrategyParams().mid_edge
    assert session.adaptations
    assert session.adaptations[-1][1].reason.startswith("tighten:")


def _arm_quotes(session: PaperSession, *, spot: str, strike: str, yes_bid: str, no_bid: str) -> None:
    session.armed = True
    session.ticker = _TICKER
    session.spot = Decimal(spot)
    session.strike = Decimal(strike)
    session.book.apply_snapshot(
        _snapshot(
            market_ticker=_TICKER,
            yes_dollars_fp=[[yes_bid, "10.00"]],
            no_dollars_fp=[[no_bid, "10.00"]],
        )
    )
    session._book_mono = time.monotonic()
    session._brti_mono = time.monotonic()


def _held_yes(session: PaperSession, *, price: str, count: str = "2") -> PaperIntent:
    intent = PaperIntent(
        market_ticker=_TICKER,
        outcome="yes",
        price=Decimal(price),
        count=Decimal(count),
        style="maker",
    )
    intent.remaining = Decimal("0")
    intent.opened_ms = 1
    intent.entry_features = [0.1, 0.0, 0.5, 0.0, 0.05, 0.2, 1.0, -0.05]
    intent.fills = [
        PaperFill(
            market_ticker=_TICKER,
            style="maker",
            outcome="yes",
            price=Decimal(price),
            count=Decimal(count),
            fee=Decimal("0"),
            ts_ms=1,
            source="trade",
            action="buy",
        )
    ]
    session.account.ledger.add(intent)
    session.account.cash -= Decimal(price) * Decimal(count)
    return intent


def test_session_sells_inside_the_window_on_signal_flip(tmp_path) -> None:
    session = _session(tmp_path)
    intent = _held_yes(session, price="0.40")
    _arm_quotes(session, spot="100", strike="200000", yes_bid="0.4200", no_bid="0.5000")
    session._on_quote(10_000, _IN_WINDOW)
    assert intent.status == "closed"
    assert intent.exit_reason == "signal_flip"
    assert intent.pnl is not None
    assert session.learner.n == 1
    assert "signal_flip" in session.learner.last_reason
    view = session.view(_IN_WINDOW)
    assert view["open_trades"] == []
    assert view["closed_trades"][0]["exit_reason"] == "signal_flip"
    assert view["learner"]["samples"] == 1
    session.save()
    loaded = PaperSession.load(session.root)
    assert loaded is not None
    assert loaded.learner.n == 1
    assert loaded.account.realized_pnl() == intent.pnl


def test_session_stop_adapts_before_four_settlements(tmp_path) -> None:
    session = _session(tmp_path)
    before = session.params.contracts
    intent = _held_yes(session, price="0.55")
    _arm_quotes(session, spot="200000", strike="100", yes_bid="0.3000", no_bid="0.5500")
    session._on_quote(10_000, _IN_WINDOW)
    assert intent.exit_reason == "stop"
    assert intent.pnl is not None and intent.pnl < 0
    assert session.params.contracts < before
    assert session.adaptations
    assert session.adaptations[-1][1].reason.startswith("tighten:")
    assert "rolling pnl negative" in session.adaptations[-1][1].reason


def test_session_holds_a_live_thesis_until_settlement(tmp_path) -> None:
    session = _session(tmp_path)
    intent = _held_yes(session, price="0.50")
    _arm_quotes(session, spot="200000", strike="100", yes_bid="0.5200", no_bid="0.4500")
    session._on_quote(10_000, _IN_WINDOW)
    assert intent.status == "open"
    assert intent.exit_reason is None
    mark = session.view(_IN_WINDOW)["open_trades"][0]["mark"]
    assert mark == "0.5200"
    session.settle_ticker(
        _TICKER,
        official="yes",
        close_avg=Decimal("200000"),
        strike=Decimal("100"),
        now=_IN_WINDOW,
    )
    assert intent.status == "settled"
    assert intent.won is True
    assert intent.exit_reason == "settlement"
    assert session.account.credit_settlements() == Decimal("0")
    assert session.account.realized_pnl() == intent.pnl
    assert intent.pnl is not None and intent.pnl > 0


def test_session_cancels_a_wrong_sided_resting_maker(tmp_path) -> None:
    session = _session(tmp_path)
    intent = PaperIntent(
        market_ticker=_TICKER,
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("5"),
        style="maker",
    )
    session.account.ledger.add(intent)
    session.last_entry_ms = 5_000
    _arm_quotes(session, spot="100", strike="200000", yes_bid="0.4200", no_bid="0.5000")
    session._on_quote(5_000, _IN_WINDOW)
    assert intent.status == "cancelled"
    assert intent.exit_reason == "cancel_signal_flip"
    assert intent.remaining == Decimal("0")
    assert session.account.cash == Decimal("1000")
    assert session.learner.n == 0


def test_open_mark_drawdown_tightens_once_per_position(tmp_path) -> None:
    session = _session(tmp_path)
    session.armed = True
    intent = _held_yes(session, price="0.60", count="20")
    session.book.apply_snapshot(
        _snapshot(
            market_ticker=_TICKER,
            yes_dollars_fp=[["0.1000", "30.00"]],
            no_dollars_fp=[["0.2000", "10.00"]],
        )
    )
    before = session.params.contracts
    session._maybe_mark_adapt(_IN_WINDOW)
    assert session.params.contracts < before
    assert "open mark drawdown" in session.adaptations[-1][1].reason
    stopped = session.params.contracts
    session._last_mark_adapt_mono = None
    session._maybe_mark_adapt(_IN_WINDOW)
    assert session.params.contracts == stopped
    assert intent.status == "open"


def test_second_session_lock_is_refused(tmp_path) -> None:
    path = tmp_path / "session.lock"
    first = SessionLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="paper session lock"):
            SessionLock(path).acquire()
    finally:
        first.release()


def test_parse_official_result_ignores_scalar_and_empty() -> None:
    assert parse_official_result({"market": {"result": "yes"}}) == "yes"
    assert parse_official_result({"market": {"result": "no"}}) == "no"
    assert parse_official_result({"market": {"result": ""}}) is None
    assert parse_official_result({"market": {"result": "scalar"}}) is None


def test_paper_cli_defaults_and_refuses_non_paper_trade_env() -> None:
    args = build_paper_parser().parse_args([])
    assert args.bankroll == "1000"
    assert args.hours == 24
    assert args.target_return == "0.50"
    assert_paper_only("paper")
    with pytest.raises(RuntimeError, match="KALSHI_TRADE_ENV"):
        assert_paper_only("demo")


def test_dashboard_reads_session_and_renders_panels(tmp_path) -> None:
    session = _session(tmp_path)
    session.save()
    jsonl = tmp_path / "cap.jsonl"
    jsonl.write_text("", encoding="utf-8")
    feed = LiveFeed(
        jsonl_path=jsonl,
        lock_path=tmp_path / "recorder.lock",
        session_dir=session.root,
    )
    snap = feed.snapshot()
    assert snap["paper_session"]["bankroll"] == "1000"
    assert snap["paper_session"]["target_return"] == "0.50"
    assert snap["paper_session"]["strategy"] == "signal-edge"
    assert snap["paper_session"]["running"] is False
    assert snap["paper_session"]["trade_env"] == "paper"

    app = create_app(
        jsonl_path=jsonl,
        lock_path=tmp_path / "dash.lock",
        session_dir=session.root,
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="paper-equity"' in page.text
        assert 'id="paper-fills"' in page.text
        assert 'id="paper-learn"' in page.text
        body = client.get("/api/state").json()
        assert body["paper_session"]["equity"] == "1000"
        assert body["paper_session"]["learner"]["samples"] == 0
        assert body["paper_session"]["learner"]["active"] is False
        assert body["paper_session"]["ends_at_amsterdam"].endswith("CEST")
        script = client.get("/static/dashboard.js")
        assert "Europe/Amsterdam" in script.text
