"""Signal-edge paper strategy. Params adapt; the +50% target is only a KPI.

Defaults match ``signal._hint`` (8¢ mid-window, 3¢ last minute). Last-minute
takes also wait for 15 close ticks, the same gate ``replay`` uses so the first
close print cannot lock a contract.

Open positions can close inside the window. A loss or a bad open mark tightens
before four settlements have stacked up. Size is not raised to chase a deficit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal, Never

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState, Side
from kalshi_bot.paper import PaperStyle
from kalshi_bot.signal import Signal

STRATEGY_NAME = "signal-edge"
_MIN_TAKE_CLOSE_WINDOW = 15
_MIN_CONTRACTS = Decimal("1")
_MAX_CONTRACTS = Decimal("25")
_DEFAULT_MID_EDGE = Decimal("0.08")
_DEFAULT_LAST_EDGE = Decimal("0.03")
_MAX_MID_EDGE = Decimal("0.20")
_MAX_LAST_EDGE = Decimal("0.12")
_MIN_COOLDOWN_S = 10.0
_MAX_COOLDOWN_S = 300.0
_MIN_OPEN_RISK = Decimal("5")
_BEHIND_BANKROLL = Decimal("0.02")
_HIT_WEAK = Decimal("0.45")
_HIT_HEALTHY = Decimal("0.55")
_ADAPT_WINDOW = 8
_DEFAULT_STOP = Decimal("0.08")
_DEFAULT_TAKE = Decimal("0.06")
_DEFAULT_FLIP = Decimal("0.05")
_MIN_STOP = Decimal("0.04")
_MIN_TAKE = Decimal("0.03")
_EXIT_SLIPPAGE = Decimal("0.02")
_MIN_SELL = Decimal("0.01")
_FEATURE_WINDOW_S = 900.0
_BLOCK_NOTES = frozenset(
    {
        "market closed",
        "market not open",
        "ticker mismatch",
        "book invalid",
        "stale data",
        "need BRTI and strike",
    }
)


@dataclass(frozen=True)
class StrategyParams:
    """Tunables the session may change. ``risk_ceiling`` is the session cap."""

    mid_edge: Decimal = _DEFAULT_MID_EDGE
    last_minute_edge: Decimal = _DEFAULT_LAST_EDGE
    contracts: Decimal = Decimal("5")
    maker_bias: Decimal = Decimal("0.35")
    cooldown_s: float = 20.0
    max_open_risk: Decimal = Decimal("50")
    risk_ceiling: Decimal = Decimal("50")
    name: str = STRATEGY_NAME
    stop_loss: Decimal = _DEFAULT_STOP
    take_profit: Decimal = _DEFAULT_TAKE
    flip_margin: Decimal = _DEFAULT_FLIP

    def to_dict(self) -> dict[str, str | float]:
        return {
            "name": self.name,
            "mid_edge": format(self.mid_edge, "f"),
            "last_minute_edge": format(self.last_minute_edge, "f"),
            "contracts": format(self.contracts, "f"),
            "maker_bias": format(self.maker_bias, "f"),
            "cooldown_s": self.cooldown_s,
            "max_open_risk": format(self.max_open_risk, "f"),
            "risk_ceiling": format(self.risk_ceiling, "f"),
            "stop_loss": format(self.stop_loss, "f"),
            "take_profit": format(self.take_profit, "f"),
            "flip_margin": format(self.flip_margin, "f"),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str | float]) -> StrategyParams:
        return cls(
            mid_edge=Decimal(str(raw["mid_edge"])),
            last_minute_edge=Decimal(str(raw["last_minute_edge"])),
            contracts=Decimal(str(raw["contracts"])),
            maker_bias=Decimal(str(raw["maker_bias"])),
            cooldown_s=float(raw["cooldown_s"]),
            max_open_risk=Decimal(str(raw["max_open_risk"])),
            risk_ceiling=Decimal(str(raw["risk_ceiling"])),
            name=str(raw.get("name") or STRATEGY_NAME),
            stop_loss=Decimal(str(raw.get("stop_loss", _DEFAULT_STOP))),
            take_profit=Decimal(str(raw.get("take_profit", _DEFAULT_TAKE))),
            flip_margin=Decimal(str(raw.get("flip_margin", _DEFAULT_FLIP))),
        )


def initial_params(bankroll: Decimal) -> StrategyParams:
    """Five contracts and 5% of bankroll at risk. Small accounts trade 1."""
    ceiling = (bankroll * Decimal("0.05")).quantize(Decimal("0.01"))
    if ceiling < _MIN_OPEN_RISK:
        ceiling = bankroll if bankroll > 0 else _MIN_OPEN_RISK
    contracts = Decimal("5") if bankroll >= Decimal("100") else Decimal("1")
    return StrategyParams(
        contracts=contracts,
        max_open_risk=ceiling,
        risk_ceiling=ceiling,
    )


@dataclass(frozen=True)
class EntryPlan:
    """One paper order that stays inside this process."""

    outcome: Side
    style: PaperStyle
    limit: Decimal
    count: Decimal
    edge: Decimal


@dataclass(frozen=True)
class ExitPlan:
    """Sell the held side into visible bids. Not a buy of the other outcome."""

    outcome: Side
    count: Decimal
    reason: str
    min_price: Decimal = _MIN_SELL
    slippage: Decimal = _EXIT_SLIPPAGE


@dataclass(frozen=True)
class RollingScore:
    """Last settled, filled trades."""

    n: int
    wins: int
    pnl: Decimal


@dataclass(frozen=True)
class Adaptation:
    """One logged parameter change."""

    reason: str
    before: StrategyParams
    after: StrategyParams


def rolling_score_from_pnls(pnls: list[Decimal], *, window: int = _ADAPT_WINDOW) -> RollingScore:
    recent = pnls[-window:]
    wins = sum(1 for pnl in recent if pnl > 0)
    total = sum(recent, Decimal("0"))
    return RollingScore(n=len(recent), wins=wins, pnl=total)


def decide(
    signal: Signal,
    params: StrategyParams,
    book: OrderbookState,
    *,
    available_cash: Decimal,
    open_risk: Decimal,
    now_ms: int | None,
    last_entry_ms: int | None,
    close_window: int | None,
    ticker_busy: bool,
) -> EntryPlan | None:
    """One entry from model-vs-book edge, or nothing."""
    if ticker_busy or signal.model_yes is None or signal.note in _BLOCK_NOTES:
        return None
    if signal.seconds_left is not None and signal.seconds_left < 2:
        return None
    if _cooling_down(now_ms, last_entry_ms, params.cooldown_s):
        return None
    if signal.regime == "last_minute" and (close_window or 0) < _MIN_TAKE_CLOSE_WINDOW:
        return None
    bar = _edge_bar(signal.regime, params)
    plan = _best_plan(signal, params, book, bar)
    if plan is None:
        return None
    count = _size(plan, params, available_cash, open_risk)
    if count < _MIN_CONTRACTS:
        return None
    return EntryPlan(
        outcome=plan.outcome,
        style=plan.style,
        limit=plan.limit,
        count=count,
        edge=plan.edge,
    )


def exit_flags(
    signal: Signal,
    params: StrategyParams,
    book: OrderbookState,
    *,
    outcome: Side,
    filled: Decimal,
    avg_price: Decimal,
    learned: bool,
    held_ms: int | None = None,
) -> dict[str, bool] | None:
    """Independent exit predicates. None when the book cannot support an exit.

    ``learned`` is the raw learner vote. The session may still refuse to act
    on it. A stop waits two seconds so the entry spread itself is not the stop.
    """
    if filled <= 0 or signal.model_yes is None or signal.note in _BLOCK_NOTES:
        return None
    if signal.seconds_left is not None and signal.seconds_left < 2:
        return None
    if not book.valid:
        return None
    model = _model(signal, outcome)
    bid = book.best_bid(outcome)
    if model is None or bid is None:
        return None
    ask = book.implied_ask(outcome)
    mid = (bid + ask) / 2 if ask is not None else bid
    fee = quadratic_taker_fee(Decimal("1"), bid)
    flip_at = Decimal("0.5") - params.flip_margin
    stop_ready = held_ms is None or held_ms >= 2000
    return {
        "signal_flip": model < flip_at,
        "stop": bool(stop_ready and mid <= avg_price - params.stop_loss),
        "learned": learned,
        "edge_gone": bid - fee >= model,
        "take_profit": bid >= avg_price + params.take_profit,
    }


def decide_exit(
    signal: Signal,
    params: StrategyParams,
    book: OrderbookState,
    *,
    outcome: Side,
    filled: Decimal,
    avg_price: Decimal,
    learn_exit: bool,
    held_ms: int | None = None,
) -> ExitPlan | None:
    """Close a live position, or keep it for settlement.

    Order is signal flip, adverse mid, a learned loss pattern, edge gone
    (bid through the model after the taker fee), then a realizable take-profit.
    ``learn_exit`` is whether a learned exit may place an order. A stale or
    closed book does not exit.
    """
    flags = exit_flags(
        signal,
        params,
        book,
        outcome=outcome,
        filled=filled,
        avg_price=avg_price,
        learned=learn_exit,
        held_ms=held_ms,
    )
    if flags is None:
        return None
    if flags["signal_flip"]:
        reason = "signal_flip"
    elif flags["stop"]:
        reason = "stop"
    elif flags["learned"]:
        reason = "learned"
    elif flags["edge_gone"]:
        reason = "edge_gone"
    elif flags["take_profit"]:
        reason = "take_profit"
    else:
        return None
    return ExitPlan(outcome=outcome, count=filled, reason=reason)


def cancel_reason(
    signal: Signal,
    params: StrategyParams,
    *,
    outcome: Side,
    style: PaperStyle,
    price: Decimal,
) -> str | None:
    """Drop a resting buy whose side or edge is no longer the thesis."""
    if signal.model_yes is None or signal.note in _BLOCK_NOTES:
        return None
    model = _model(signal, outcome)
    if model is None:
        return None
    if model < Decimal("0.5") - params.flip_margin:
        return "cancel_signal_flip"
    bar = _edge_bar(signal.regime, params)
    if style == "maker":
        if model - price < bar:
            return "cancel_edge_gone"
    elif style == "taker":
        edge = signal.yes_edge if outcome == "yes" else signal.no_edge
        if edge is None or edge < bar:
            return "cancel_edge_gone"
    else:
        unreachable: Never = style
        raise ValueError(f"unknown style {unreachable!r}")
    return None


def market_features(
    signal: Signal,
    book: OrderbookState,
    outcome: Side,
    style: PaperStyle,
    price: Decimal,
) -> list[float]:
    """Entry-time features. Hold time and the exit reason are labels, not inputs."""
    model = _model(signal, outcome)
    if outcome == "yes":
        edge = signal.yes_edge
    elif outcome == "no":
        edge = signal.no_edge
    else:
        unreachable: Never = outcome
        raise ValueError(f"unknown outcome {unreachable!r}")
    seconds = 0.0 if signal.seconds_left is None else signal.seconds_left / _FEATURE_WINDOW_S
    bid_sz, ask_sz, spread = _touch(book, outcome)
    depth_total = bid_sz + ask_sz
    imbalance = 0.0 if depth_total <= 0 else float((bid_sz - ask_sz) / depth_total)
    depth = math.log1p(float(depth_total)) / math.log1p(50.0)
    if style == "maker":
        maker = 1.0
    elif style == "taker":
        maker = 0.0
    else:
        unreachable_style: Never = style
        raise ValueError(f"unknown style {unreachable_style!r}")
    versus = 0.0 if model is None else float(price - model)
    return [
        0.0 if edge is None else float(edge),
        1.0 if signal.regime == "last_minute" else 0.0,
        min(1.0, max(0.0, seconds)),
        min(1.0, max(-1.0, imbalance)),
        min(1.0, max(0.0, spread)),
        min(1.0, max(0.0, depth)),
        maker,
        min(0.5, max(-0.5, versus)),
    ]


def adapt(
    params: StrategyParams,
    *,
    score: RollingScore,
    equity: Decimal,
    bankroll: Decimal,
    target_return: Decimal,
    elapsed_s: float,
    duration_s: float,
    min_samples: int = 4,
    allow_loosen: bool = True,
) -> Adaptation | None:
    """Tighten on a losing close immediately. Loosen only after a full window.

    The +50% pace still cannot raise size. An empty score does nothing.
    """
    if score.n >= 1:
        hit = Decimal(score.wins) / Decimal(score.n)
        if score.pnl < 0 or hit < _HIT_WEAK:
            return _tighten(params, behind=False, hit=hit, pnl=score.pnl, mark=False)
    if score.n < min_samples:
        return None
    behind = _behind_pace(
        equity=equity,
        bankroll=bankroll,
        target_return=target_return,
        elapsed_s=elapsed_s,
        duration_s=duration_s,
    )
    hit = Decimal(score.wins) / Decimal(score.n)
    rolling_bad = score.pnl < 0 or hit < _HIT_WEAK
    if behind or rolling_bad:
        return _tighten(params, behind=behind, hit=hit, pnl=score.pnl, mark=False)
    if allow_loosen and hit >= _HIT_HEALTHY and score.pnl > 0:
        return _loosen(params)
    return None


def adapt_to_mark(
    params: StrategyParams,
    *,
    unrealized: Decimal,
    drawdown: Decimal,
) -> Adaptation | None:
    """One defensive step when the open mark is through ``drawdown`` dollars."""
    if drawdown <= 0 or unrealized > -drawdown:
        return None
    return _tighten(
        params,
        behind=False,
        hit=Decimal("1"),
        pnl=Decimal("0"),
        mark=True,
    )


def _edge_bar(regime: Literal["mid", "last_minute"], params: StrategyParams) -> Decimal:
    if regime == "last_minute":
        return params.last_minute_edge
    if regime == "mid":
        return params.mid_edge
    unreachable: Never = regime
    raise ValueError(f"unknown regime {unreachable!r}")


def _cooling_down(now_ms: int | None, last_entry_ms: int | None, cooldown_s: float) -> bool:
    if now_ms is None or last_entry_ms is None:
        return False
    return (now_ms - last_entry_ms) < int(cooldown_s * 1000)


@dataclass(frozen=True)
class _Candidate:
    outcome: Side
    style: PaperStyle
    limit: Decimal
    edge: Decimal
    visible: Decimal


def _best_plan(
    signal: Signal,
    params: StrategyParams,
    book: OrderbookState,
    bar: Decimal,
) -> _Candidate | None:
    best: _Candidate | None = None
    for outcome in ("yes", "no"):
        candidate = _candidate(signal, params, book, bar, outcome)
        if candidate is None:
            continue
        if best is None or candidate.edge > best.edge:
            best = candidate
    return best


def _candidate(
    signal: Signal,
    params: StrategyParams,
    book: OrderbookState,
    bar: Decimal,
    outcome: Side,
) -> _Candidate | None:
    model = _model(signal, outcome)
    if model is None:
        return None
    taker_edge = signal.yes_edge if outcome == "yes" else signal.no_edge
    bid = book.best_bid(outcome) if book.valid else None
    maker_edge = (model - bid) if bid is not None else None
    taker_ok = taker_edge is not None and taker_edge >= bar
    maker_ok = maker_edge is not None and bid is not None and maker_edge >= bar
    prefer_maker = params.maker_bias >= Decimal("0.5")
    if prefer_maker and maker_ok and bid is not None and maker_edge is not None:
        return _Candidate(outcome, "maker", bid, maker_edge, params.contracts)
    if taker_ok and taker_edge is not None:
        walked = _visible_taker(book, outcome, model, bar)
        if walked is not None:
            limit, visible = walked
            return _Candidate(outcome, "taker", limit, taker_edge, visible)
    if maker_ok and bid is not None and maker_edge is not None:
        return _Candidate(outcome, "maker", bid, maker_edge, params.contracts)
    return None


def _model(signal: Signal, outcome: Side) -> Decimal | None:
    if signal.model_yes is None:
        return None
    if outcome == "yes":
        return signal.model_yes
    if outcome == "no":
        return Decimal("1") - signal.model_yes
    unreachable: Never = outcome
    raise ValueError(f"unknown outcome {unreachable!r}")


def _visible_taker(
    book: OrderbookState,
    outcome: Side,
    model: Decimal,
    bar: Decimal,
) -> tuple[Decimal, Decimal] | None:
    """Worst ask that still clears ``bar``, and the size sitting at those asks."""
    if not book.valid:
        return None
    worst: Decimal | None = None
    total = Decimal("0")
    for ask, size in book.ask_levels(outcome):
        if size <= 0:
            continue
        edge = model - ask - quadratic_taker_fee(Decimal("1"), ask)
        if edge < bar:
            break
        worst = ask
        total += size
    if worst is None or total <= 0:
        return None
    return worst, total


def _size(
    plan: _Candidate,
    params: StrategyParams,
    available_cash: Decimal,
    open_risk: Decimal,
) -> Decimal:
    unit = plan.limit
    if plan.style == "taker":
        unit += quadratic_taker_fee(Decimal("1"), plan.limit)
    elif plan.style == "maker":
        unit += Decimal("0")
    else:
        unreachable: Never = plan.style
        raise ValueError(f"unknown style {unreachable!r}")
    if unit <= 0 or available_cash <= 0:
        return Decimal("0")
    room = params.max_open_risk - open_risk
    if room <= 0:
        return Decimal("0")
    by_cash = (available_cash / unit).to_integral_value(rounding=ROUND_DOWN)
    by_risk = (room / unit).to_integral_value(rounding=ROUND_DOWN)
    by_book = plan.visible.to_integral_value(rounding=ROUND_DOWN)
    count = min(params.contracts, by_cash, by_risk, by_book)
    if count < _MIN_CONTRACTS:
        return Decimal("0")
    return count


def _behind_pace(
    *,
    equity: Decimal,
    bankroll: Decimal,
    target_return: Decimal,
    elapsed_s: float,
    duration_s: float,
) -> bool:
    """True when equity is short of a linear path to ``bankroll * (1 + target)``."""
    if duration_s <= 0:
        frac = Decimal("1")
    else:
        frac = Decimal(str(elapsed_s)) / Decimal(str(duration_s))
        if frac < 0:
            frac = Decimal("0")
        if frac > 1:
            frac = Decimal("1")
    target_equity = bankroll * (Decimal("1") + target_return)
    pace = bankroll + (target_equity - bankroll) * frac
    return (pace - equity) > (bankroll * _BEHIND_BANKROLL)


def _tighten(
    params: StrategyParams,
    *,
    behind: bool,
    hit: Decimal,
    pnl: Decimal,
    mark: bool,
) -> Adaptation | None:
    reasons: list[str] = []
    if behind:
        reasons.append("behind aspirational pace")
    if pnl < 0:
        reasons.append("rolling pnl negative")
    if hit < _HIT_WEAK:
        reasons.append(f"hit rate {hit.quantize(Decimal('0.01'))}")
    if mark:
        reasons.append("open mark drawdown")
    after = StrategyParams(
        mid_edge=min(_MAX_MID_EDGE, params.mid_edge + Decimal("0.01")),
        last_minute_edge=min(_MAX_LAST_EDGE, params.last_minute_edge + Decimal("0.005")),
        contracts=max(_MIN_CONTRACTS, params.contracts - 1),
        maker_bias=min(Decimal("1"), params.maker_bias + Decimal("0.15")),
        cooldown_s=min(_MAX_COOLDOWN_S, params.cooldown_s * 1.5),
        max_open_risk=max(
            _floor_risk(params),
            (params.max_open_risk * Decimal("0.8")).quantize(Decimal("0.01")),
        ),
        risk_ceiling=params.risk_ceiling,
        name=params.name,
        stop_loss=max(_MIN_STOP, params.stop_loss - Decimal("0.01")),
        take_profit=max(_MIN_TAKE, params.take_profit - Decimal("0.01")),
        flip_margin=params.flip_margin,
    )
    if after == params:
        return None
    reason = "tighten: " + ", ".join(reasons) + "; raising edge and cutting size"
    return Adaptation(reason=reason, before=params, after=after)


def _loosen(params: StrategyParams) -> Adaptation | None:
    restored = params.max_open_risk
    if params.max_open_risk < params.risk_ceiling:
        restored = min(
            params.risk_ceiling,
            (params.max_open_risk / Decimal("0.8")).quantize(Decimal("0.01")),
        )
    after = StrategyParams(
        mid_edge=max(_DEFAULT_MID_EDGE, params.mid_edge - Decimal("0.005")),
        last_minute_edge=max(_DEFAULT_LAST_EDGE, params.last_minute_edge - Decimal("0.005")),
        contracts=min(_MAX_CONTRACTS, params.contracts + 1),
        maker_bias=max(Decimal("0"), params.maker_bias - Decimal("0.05")),
        cooldown_s=max(_MIN_COOLDOWN_S, params.cooldown_s / 1.5),
        max_open_risk=restored,
        risk_ceiling=params.risk_ceiling,
        name=params.name,
        stop_loss=min(_DEFAULT_STOP, params.stop_loss + Decimal("0.01")),
        take_profit=min(_DEFAULT_TAKE, params.take_profit + Decimal("0.01")),
        flip_margin=params.flip_margin,
    )
    if after == params:
        return None
    return Adaptation(
        reason="loosen: rolling pnl positive and hit rate healthy; easing toward default edges",
        before=params,
        after=after,
    )


def _touch(
    book: OrderbookState,
    outcome: Side,
) -> tuple[Decimal, Decimal, float]:
    if not book.valid:
        return Decimal("0"), Decimal("0"), 0.0
    bid = book.best_bid(outcome)
    ask = book.implied_ask(outcome)
    bid_sz = book.size_at(outcome, bid) if bid is not None else Decimal("0")
    ask_sz = Decimal("0")
    if ask is not None:
        opposite: Side = "no" if outcome == "yes" else "yes"
        ask_sz = book.size_at(opposite, Decimal("1") - ask)
    spread = float(ask - bid) if ask is not None and bid is not None else 0.0
    return bid_sz, ask_sz, spread


def _floor_risk(params: StrategyParams) -> Decimal:
    floor = min(_MIN_OPEN_RISK, params.risk_ceiling)
    if floor <= 0:
        return Decimal("0.01")
    return floor
