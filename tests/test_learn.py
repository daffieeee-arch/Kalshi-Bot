"""Online learner: cold start, one update per close, then a smaller entry."""

from __future__ import annotations

from decimal import Decimal

from kalshi_bot.learn import N_FEATURES, OnlineLearner
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.strategy import StrategyParams, decide
from test_orderbook import _snapshot
from test_strategy import _signal


def _features() -> list[float]:
    return [0.20, 0.0, 0.5, 0.1, 0.08, 0.4, 0.0, -0.10]


def _deep_book() -> OrderbookState:
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.5500", "20.00"]],
            no_dollars_fp=[["0.6000", "20.00"]],
        )
    )
    return book


def _losing_learner() -> OnlineLearner:
    learner = OnlineLearner.cold()
    features = _features()
    for _ in range(40):
        learner.update(
            features,
            won=False,
            pnl=Decimal("-0.40"),
            style="taker",
            reason="stop",
        )
    return learner


def test_learner_stays_neutral_until_four_closes() -> None:
    learner = OnlineLearner.cold()
    features = _features()
    assert learner.wants_exit(features) is False
    assert learner.adjust_params(StrategyParams(), features) == StrategyParams()
    for _ in range(3):
        learner.update(features, won=False, pnl=Decimal("-0.10"), style="taker", reason="stop")
    assert learner.n == 3
    assert learner.summary()["active"] is False
    assert learner.adjust_params(StrategyParams(), features) == StrategyParams()


def test_learner_update_cuts_the_next_entry() -> None:
    learner = _losing_learner()
    assert learner.n == 40
    assert learner.predict(_features()) < 0.40
    assert learner.wants_exit(_features()) is True
    tuned = learner.adjust_params(StrategyParams(contracts=Decimal("5")), _features())
    assert tuned.contracts == Decimal("1")
    bare = decide(
        _signal(),
        StrategyParams(contracts=Decimal("5")),
        _deep_book(),
        available_cash=Decimal("1000"),
        open_risk=Decimal("0"),
        now_ms=1_000,
        last_entry_ms=None,
        close_window=None,
        ticker_busy=False,
    )
    tilted = decide(
        _signal(),
        tuned,
        _deep_book(),
        available_cash=Decimal("1000"),
        open_risk=Decimal("0"),
        now_ms=1_000,
        last_entry_ms=None,
        close_window=None,
        ticker_busy=False,
    )
    assert bare is not None and tilted is not None
    assert bare.count == Decimal("5")
    assert tilted.count == Decimal("1")
    assert len(_features()) == N_FEATURES


def test_learner_roundtrip_keeps_weights() -> None:
    learner = _losing_learner()
    loaded = OnlineLearner.from_dict(learner.to_dict())
    assert loaded.n == learner.n
    assert abs(loaded.predict(_features()) - learner.predict(_features())) < 1e-6
    assert OnlineLearner.from_dict(None).n == 0
    assert OnlineLearner.from_dict({"weights": [1.0]}).n == 0
