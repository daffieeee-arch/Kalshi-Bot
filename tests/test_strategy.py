"""Signal-edge entries and the aspirational-pace adaptation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.signal import Signal
from kalshi_bot.strategy import RollingScore, StrategyParams, adapt, decide
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


def test_adapt_needs_a_sample() -> None:
    assert (
        adapt(
            StrategyParams(),
            score=RollingScore(n=3, wins=0, pnl=Decimal("-1")),
            equity=Decimal("900"),
            bankroll=Decimal("1000"),
            target_return=Decimal("0.50"),
            elapsed_s=12 * 3600,
            duration_s=24 * 3600,
        )
        is None
    )


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
