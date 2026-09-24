"""Signal-edge paper strategy. Params adapt; the +50% target is only a KPI.

Defaults match ``signal._hint`` (8¢ mid-window, 3¢ last minute). Last-minute
takes also wait for 15 close ticks, the same gate ``replay`` uses so the first
close print cannot lock a contract.
"""

from __future__ import annotations

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
) -> Adaptation | None:
    """Tighten when the aspirational pace or the tape says so. Never chase a deficit."""
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
        return _tighten(params, behind=behind, hit=hit, pnl=score.pnl)
    if hit >= _HIT_HEALTHY and score.pnl > 0:
        return _loosen(params)
    return None


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
) -> Adaptation | None:
    reasons: list[str] = []
    if behind:
        reasons.append("behind aspirational pace")
    if pnl < 0:
        reasons.append("rolling pnl negative")
    if hit < _HIT_WEAK:
        reasons.append(f"hit rate {hit.quantize(Decimal('0.01'))}")
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
    )
    if after == params:
        return None
    return Adaptation(
        reason="loosen: rolling pnl positive and hit rate healthy; easing toward default edges",
        before=params,
        after=after,
    )


def _floor_risk(params: StrategyParams) -> Decimal:
    floor = min(_MIN_OPEN_RISK, params.risk_ceiling)
    if floor <= 0:
        return Decimal("0.01")
    return floor
