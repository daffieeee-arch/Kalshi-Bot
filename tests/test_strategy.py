"""Signal-edge entries and the aspirational-pace adaptation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.signal import Signal
from kalshi_bot.strategy import RollingScore, StrategyParams, adapt, adapt_to_mark, decide, decide_exit
from test_orderbook import _snapshot


def _signal(**overrides: object) -> Signal:
    ask = Decimal("0.40")
    model = Decimal("0.70")
    edge = model - ask - quadratic_taker_fee(Decimal("1"), ask)
    base = Signal(
        regime="mid",
        spot=Decimal("101"),
        strike=Decimal("100"),
        gap=Decimal("1"),
        seconds_left=400,
        sigma=Decimal("6.7"),
        model_yes=model,
        yes_ask=ask,
        no_ask=Decimal("0.70"),
        yes_edge=edge,
        no_edge=Decimal("-1"),
        hint="paper YES?",
        note="mid-window",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _book() -> OrderbookState:
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.5500", "10.00"]],
            no_dollars_fp=[["0.6000", "4.00"]],
        )
    )
    return book


def test_decide_sizes_taker_to_visible_ask() -> None:
    plan = decide(
        _signal(),
        StrategyParams(),
        _book(),
        available_cash=Decimal("1000"),
        open_risk=Decimal("0"),
        now_ms=1_000,
        last_entry_ms=None,
        close_window=None,
        ticker_busy=False,
    )
    assert plan is not None
    assert plan.style == "taker"
    assert plan.outcome == "yes"
    assert plan.limit == Decimal("0.40")
    assert plan.count == Decimal("4")


def test_decide_waits_when_the_book_is_blocked() -> None:
    plan = decide(
        _signal(note="stale data", hint="wait"),
        StrategyParams(),
        _book(),
        available_cash=Decimal("1000"),
        open_risk=Decimal("0"),
        now_ms=1_000,
        last_entry_ms=None,
        close_window=None,
        ticker_busy=False,
    )
    assert plan is None


def test_adapt_ignores_an_empty_score() -> None:
    assert (
        adapt(
            StrategyParams(),
            score=RollingScore(n=0, wins=0, pnl=Decimal("0")),
            equity=Decimal("900"),
            bankroll=Decimal("1000"),
            target_return=Decimal("0.50"),
            elapsed_s=12 * 3600,
            duration_s=24 * 3600,
        )
        is None
    )


def test_adapt_tightens_on_the_first_losing_close() -> None:
    params = StrategyParams()
    change = adapt(
        params,
        score=RollingScore(n=1, wins=0, pnl=Decimal("-0.40")),
        equity=Decimal("999.60"),
        bankroll=Decimal("1000"),
        target_return=Decimal("0.50"),
        elapsed_s=30 * 60,
        duration_s=24 * 3600,
    )
    assert change is not None
    assert change.after.contracts < params.contracts
    assert change.after.mid_edge > params.mid_edge
    assert change.after.stop_loss < params.stop_loss
    assert "rolling pnl negative" in change.reason


def test_adapt_tightens_when_behind_even_if_recent_trades_won() -> None:
    params = StrategyParams()
    change = adapt(
        params,
        score=RollingScore(n=4, wins=4, pnl=Decimal("10")),
        equity=Decimal("1000"),
        bankroll=Decimal("1000"),
        target_return=Decimal("0.50"),
        elapsed_s=12 * 3600,
        duration_s=24 * 3600,
    )
    assert change is not None
    assert change.after.contracts < params.contracts
    assert change.after.mid_edge > params.mid_edge
    assert change.after.max_open_risk <= params.max_open_risk
    assert change.after.maker_bias > params.maker_bias
    assert "behind aspirational pace" in change.reason
    assert "cutting size" in change.reason


def test_decide_exit_signal_flip_stop_and_take_profit() -> None:
    hold = _signal(model_yes=Decimal("0.72"), yes_edge=Decimal("0.20"), no_edge=Decimal("-0.2"))
    assert (
        decide_exit(
            hold,
            StrategyParams(),
            _exit_book(bid="0.4200"),
            outcome="yes",
            filled=Decimal("2"),
            avg_price=Decimal("0.40"),
            learn_exit=False,
        )
        is None
    )
    flipped = decide_exit(
        _signal(model_yes=Decimal("0.20")),
        StrategyParams(),
        _book(),
        outcome="yes",
        filled=Decimal("2"),
        avg_price=Decimal("0.40"),
        learn_exit=False,
    )
    assert flipped is not None
    assert flipped.reason == "signal_flip"
    stopped = decide_exit(
        hold,
        StrategyParams(),
        _book(),
        outcome="yes",
        filled=Decimal("2"),
        avg_price=Decimal("0.70"),
        learn_exit=False,
        held_ms=5_000,
    )
    assert stopped is not None
    assert stopped.reason == "stop"
    assert (
        decide_exit(
            hold,
            StrategyParams(),
            _book(),
            outcome="yes",
            filled=Decimal("2"),
            avg_price=Decimal("0.70"),
            learn_exit=False,
            held_ms=500,
        )
        is None
    )
    banked = decide_exit(
        hold,
        StrategyParams(),
        _exit_book(bid="0.5200"),
        outcome="yes",
        filled=Decimal("2"),
        avg_price=Decimal("0.40"),
        learn_exit=False,
    )
    assert banked is not None
    assert banked.reason == "take_profit"
    assert (
        decide_exit(
            _signal(seconds_left=1.0),
            StrategyParams(),
            _exit_book(bid="0.1000"),
            outcome="yes",
            filled=Decimal("2"),
            avg_price=Decimal("0.70"),
            learn_exit=False,
            held_ms=10_000,
        )
        is None
    )


def test_decide_exit_learned_and_edge_gone() -> None:
    # Bid 0.55 is through a 0.48 model after the taker fee, and not a flip or a stop.
    gone = decide_exit(
        _signal(model_yes=Decimal("0.48"), yes_edge=Decimal("0"), no_edge=Decimal("-0.2")),
        StrategyParams(),
        _exit_book(bid="0.5500"),
        outcome="yes",
        filled=Decimal("1"),
        avg_price=Decimal("0.50"),
        learn_exit=False,
    )
    assert gone is not None
    assert gone.reason == "edge_gone"
    learned = decide_exit(
        _signal(model_yes=Decimal("0.72"), yes_edge=Decimal("0.20"), no_edge=Decimal("-0.2")),
        StrategyParams(),
        _book(),
        outcome="yes",
        filled=Decimal("1"),
        avg_price=Decimal("0.40"),
        learn_exit=True,
    )
    assert learned is not None
    assert learned.reason == "learned"


def test_adapt_to_mark_tightens_once_per_call() -> None:
    params = StrategyParams()
    change = adapt_to_mark(params, unrealized=Decimal("-6"), drawdown=Decimal("5"))
    assert change is not None
    assert "open mark drawdown" in change.reason
    assert change.after.contracts < params.contracts
    assert adapt_to_mark(params, unrealized=Decimal("-1"), drawdown=Decimal("5")) is None


def test_old_params_payload_defaults_exit_thresholds() -> None:
    raw = StrategyParams().to_dict()
    del raw["stop_loss"]
    del raw["take_profit"]
    del raw["flip_margin"]
    loaded = StrategyParams.from_dict(raw)
    assert loaded.stop_loss == Decimal("0.08")
    assert loaded.take_profit == Decimal("0.06")


def _exit_book(bid: str) -> OrderbookState:
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[[bid, "10.00"]],
            no_dollars_fp=[["0.4000", "10.00"]],
        )
    )
    return book


def test_adapt_can_loosen_when_ahead_and_the_tape_is_healthy() -> None:
    params = StrategyParams()
    change = adapt(
        params,
        score=RollingScore(n=8, wins=6, pnl=Decimal("4")),
        equity=Decimal("1300"),
        bankroll=Decimal("1000"),
        target_return=Decimal("0.50"),
        elapsed_s=12 * 3600,
        duration_s=24 * 3600,
    )
    assert change is not None
    assert change.after.contracts == params.contracts + 1
    assert change.after.mid_edge == params.mid_edge
    assert change.after.max_open_risk <= params.risk_ceiling
