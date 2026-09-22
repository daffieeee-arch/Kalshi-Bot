"""Local probability marks; not a claim that the model has edge."""

from __future__ import annotations

from decimal import Decimal

from kalshi_bot.signal import evaluate, last_minute_yes_prob, midwindow_yes_prob, realized_sigma


def test_last_minute_locked_close_is_certain() -> None:
    assert last_minute_yes_prob(Decimal("100"), 60, Decimal("100"), Decimal("99"), Decimal("5")) == 1
    assert last_minute_yes_prob(Decimal("98"), 60, Decimal("100"), Decimal("99"), Decimal("5")) == 0


def test_last_minute_far_below_is_near_zero() -> None:
    p = last_minute_yes_prob(Decimal("85000"), 30, Decimal("85000"), Decimal("86000"), Decimal("6.7"))
    assert p < Decimal("0.01")


def test_midwindow_spot_above_is_not_a_lock() -> None:
    early = midwindow_yes_prob(Decimal("86100"), Decimal("86000"), 780, Decimal("6.7"))
    late = midwindow_yes_prob(Decimal("86100"), Decimal("86000"), 30, Decimal("6.7"))
    assert Decimal("0.4") < early < Decimal("0.8")
    assert late > Decimal("0.95")


def test_evaluate_last_minute_hint_is_conservative() -> None:
    signal = evaluate(
        spot=Decimal("87000"),
        strike=Decimal("86000"),
        seconds_left=20,
        close_avg=Decimal("86900"),
        close_window=20,
        yes_ask=Decimal("0.99"),
        no_ask=Decimal("0.02"),
        sigma=Decimal("6.7"),
    )
    assert signal.regime == "last_minute"
    assert signal.model_yes is not None and signal.model_yes > Decimal("0.99")
    assert signal.hint == "wait"


def test_evaluate_midwindow_needs_fatter_edge() -> None:
    signal = evaluate(
        spot=Decimal("86100"),
        strike=Decimal("86000"),
        seconds_left=600,
        close_avg=None,
        close_window=None,
        yes_ask=Decimal("0.70"),
        no_ask=Decimal("0.32"),
        sigma=Decimal("6.7"),
    )
    assert signal.regime == "mid"
    assert signal.hint == "wait"


def test_realized_sigma_falls_back_when_short() -> None:
    assert realized_sigma([Decimal("1"), Decimal("2")]) == Decimal("6.7")
