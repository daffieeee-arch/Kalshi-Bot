"""Exit-quote deltas, session metrics, and settlement Brier for paper research.

The settlement Brier uses the signal model's P(YES) at entry and the contract's
yes/no settlement. It is not the online learner's P(in-window win), and a small
sample is not evidence of edge.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Never

from kalshi_bot.learn import FEATURE_NAMES
from kalshi_bot.orderbook import OrderbookState, Side
from kalshi_bot.paper import PaperIntent, PaperStyle, position_legs
from kalshi_bot.signal import Signal
from kalshi_bot.strategy import StrategyParams, exit_flags, market_features

EXIT_PREDICATES: tuple[str, ...] = (
    "signal_flip",
    "stop",
    "learned",
    "edge_gone",
    "take_profit",
)

# Entry can clear the maker edge bar while model < 0.5 - flip_margin, so
# signal_flip is already true on that quote. This forwardtest does not change
# that rule. Instrumentation records it; a min-hold would only hide it.
FLIP_ENTRY_CAVEAT = (
    "signal_flip can already be true on the entry quote when the model is below "
    "0.5 - flip_margin while maker edge still clears the entry bar. Both arms "
    "share this. No minimum hold was added."
)
ACTIVITY_NOTE = "Less activity or a smaller size is not evidence of a better prediction."

BRIER_MODEL = "signal.model_yes"
BRIER_LABEL = "settlement_yes"
BRIER_MARKET = "entry yes mid, else yes ask"


def learner_influences(*, learn_exit_orders: bool) -> dict[str, bool]:
    """What still moves the paper session when learned-exit orders are gated."""
    return {
        "learn_exit_orders": learn_exit_orders,
        "training_on_closes": True,
        "adjust_params": True,
        "rolling_adapt": True,
        "mark_adapt": True,
        "size_and_cooldown": True,
        "stop": True,
        "signal_flip": True,
        "take_profit": True,
        "edge_gone": True,
        "settlement": True,
    }


def build_quote(
    signal: Signal,
    book: OrderbookState,
    params: StrategyParams,
    *,
    outcome: Side,
    style: PaperStyle,
    price: Decimal,
    avg_price: Decimal,
    held_ms: int | None,
    learned: bool,
    learner_p: float | None,
    ts_ms: int | None,
    filled: Decimal,
) -> dict[str, Any]:
    """One event-clock mark of the fields an exit predicate can see."""
    features = market_features(signal, book, outcome, style, price)
    flags = exit_flags(
        signal,
        params,
        book,
        outcome=outcome,
        filled=filled,
        avg_price=avg_price,
        learned=learned,
        held_ms=held_ms,
    )
    bid = book.best_bid(outcome) if book.valid else None
    ask = book.implied_ask(outcome) if book.valid else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else bid
    yes_bid = book.best_bid("yes") if book.valid else None
    yes_ask = book.implied_ask("yes") if book.valid else signal.yes_ask
    yes_mid = (yes_bid + yes_ask) / 2 if yes_bid is not None and yes_ask is not None else None
    if outcome == "yes":
        edge = signal.yes_edge
        model_p = signal.model_yes
    elif outcome == "no":
        edge = signal.no_edge
        model_p = None if signal.model_yes is None else Decimal("1") - signal.model_yes
    else:
        unreachable: Never = outcome
        raise ValueError(f"unknown outcome {unreachable!r}")
    return {
        "ts_ms": ts_ms,
        "held_ms": held_ms,
        "model_p": _num(model_p),
        "model_yes": _num(signal.model_yes),
        "bid": _num(bid),
        "ask": _num(ask),
        "mid": _num(mid),
        "edge": _num(edge),
        "seconds_left": signal.seconds_left,
        "spot": _num(signal.spot),
        "strike": _num(signal.strike),
        "gap": _num(signal.gap),
        "yes_ask": _num(yes_ask),
        "yes_bid": _num(yes_bid),
        "yes_mid": _num(yes_mid),
        "regime": signal.regime,
        "learner_p": learner_p,
        "features": features,
        "predicates": flags,
        "evaluable": flags is not None,
    }


def build_delta(
    entry: dict[str, Any] | None,
    exit_mark: dict[str, Any] | None,
    *,
    reason: str,
    hold_ms: int | None,
    learn_exit_orders: bool,
    learned_signal: bool,
) -> dict[str, Any]:
    """What changed between the entry mark and the close, on the event clock."""
    entry_flags = _flags(entry)
    exit_flags_now = _flags(exit_mark)
    flipped = [
        name
        for name in EXIT_PREDICATES
        if entry_flags.get(name) is False and exit_flags_now.get(name) is True
    ]
    already = [name for name in EXIT_PREDICATES if entry_flags.get(name) is True]
    sub_1s = hold_ms is not None and hold_ms < 1000
    return {
        "exit_reason": reason,
        "hold_ms": hold_ms,
        "sub_1s": sub_1s,
        "same_ts": hold_ms == 0,
        "learn_exit_orders": learn_exit_orders,
        "learned_signal": learned_signal,
        "learned_blocked": learned_signal and not learn_exit_orders,
        "model_p_delta": _sub(_get(exit_mark, "model_p"), _get(entry, "model_p")),
        "model_yes_delta": _sub(_get(exit_mark, "model_yes"), _get(entry, "model_yes")),
        "bid_delta": _sub(_get(exit_mark, "bid"), _get(entry, "bid")),
        "ask_delta": _sub(_get(exit_mark, "ask"), _get(entry, "ask")),
        "mid_delta": _sub(_get(exit_mark, "mid"), _get(entry, "mid")),
        "edge_delta": _sub(_get(exit_mark, "edge"), _get(entry, "edge")),
        "seconds_left_delta": _sub_float(_get(exit_mark, "seconds_left"), _get(entry, "seconds_left")),
        "spot_delta": _sub(_get(exit_mark, "spot"), _get(entry, "spot")),
        "gap_delta": _sub(_get(exit_mark, "gap"), _get(entry, "gap")),
        "learner_p_entry": _get(entry, "learner_p"),
        "learner_p_exit": _get(exit_mark, "learner_p"),
        "feature_delta": _feature_delta(_get(entry, "features"), _get(exit_mark, "features")),
        "predicates_entry": entry_flags or None,
        "predicates_exit": exit_flags_now or None,
        "predicate_flipped_true": flipped,
        "predicates_already_true_at_entry": already,
        "flip_already_true_at_entry": entry_flags.get("signal_flip") is True,
        "same_quote_contradiction": bool(
            sub_1s and reason in already and entry is not None
        ),
        "entry_quote_missing": entry is None,
    }


def prediction_row(intent: PaperIntent, quote: dict[str, Any]) -> dict[str, Any]:
    """One settlement-forecast row. The label is filled when the market resolves."""
    return {
        "ticker": intent.market_ticker,
        "outcome": intent.outcome,
        "opened_ms": intent.opened_ms,
        "model": BRIER_MODEL,
        "label": BRIER_LABEL,
        "model_note": (
            "Local signal P(YES) at the entry fill. Not the online learner. "
            "learner_p_win is P(in-window pnl > 0) and is not this Brier."
        ),
        "model_yes": quote.get("model_yes"),
        "learner_p_win": quote.get("learner_p"),
        "yes_ask": quote.get("yes_ask"),
        "yes_mid": quote.get("yes_mid"),
        "market_benchmark": BRIER_MARKET,
        "settlement_yes": None,
        "settlement_source": None,
    }


def stamp_settlement(
    rows: list[dict[str, Any]],
    ticker: str,
    *,
    yes_won: bool,
    source: str,
) -> None:
    """Fill the settlement label. An official result replaces a reconstructed one."""
    label = 1 if yes_won else 0
    for row in rows:
        if row.get("ticker") != ticker:
            continue
        if row.get("settlement_source") == "official":
            continue
        if source != "official" and row.get("settlement_yes") is not None:
            continue
        row["settlement_yes"] = label
        row["settlement_source"] = source


def brier_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Brier of signal P(YES) vs settlement, with constant-50% and market benchmarks."""
    official = [
        row
        for row in rows
        if row.get("settlement_yes") is not None
        and row.get("model_yes") is not None
        and row.get("settlement_source") == "official"
    ]
    usable = official
    note = "Official settlement only. A small n is not edge proof. Prior session B was about 0.27 on this label."
    if not usable:
        usable = [
            row
            for row in rows
            if row.get("settlement_yes") is not None and row.get("model_yes") is not None
        ]
        note = (
            "No official settlement labels yet"
            + ("; reconstructed labels are a fallback and are not the session-B Brier." if usable else ".")
            + " A small n is not edge proof."
        )
    labels = [int(row["settlement_yes"]) for row in usable]
    model_probs = [float(row["model_yes"]) for row in usable]
    market_rows = [row for row in usable if _market_prob(row) is not None]
    market_probs = [float(_market_prob(row) or 0.0) for row in market_rows]
    market_labels = [int(row["settlement_yes"]) for row in market_rows]
    correct = sum(
        1 for prob, label in zip(model_probs, labels, strict=True) if (prob >= 0.5) == (label == 1)
    )
    return {
        "n": len(usable),
        "n_official": len(official),
        "model": BRIER_MODEL,
        "label": BRIER_LABEL,
        "market_benchmark": BRIER_MARKET,
        "brier_model": _brier(model_probs, labels),
        "brier_constant_50": _brier([0.5] * len(labels), labels),
        "brier_market": _brier(market_probs, market_labels),
        "accuracy_model_at_50": None if not labels else correct / len(labels),
        "note": note,
    }


def session_metrics(
    intents: list[PaperIntent],
    *,
    bankroll: Decimal,
    equity: Decimal,
    equity_peak: Decimal,
    max_drawdown: Decimal,
    max_open: int,
    learn_exit_orders: bool,
    predictions: list[dict[str, Any]],
    now_ms: int | None,
) -> dict[str, Any]:
    """Reporting hook. Gross is raw price PnL; net subtracts fees."""
    closed = [
        intent
        for intent in intents
        if intent.status in ("closed", "settled") and intent.pnl is not None and intent.fills
    ]
    net = sum((intent.pnl or Decimal("0") for intent in closed), Decimal("0"))
    fees = Decimal("0")
    contracts = Decimal("0")
    markets: set[str] = set()
    reasons: dict[str, int] = {}
    holds: list[int] = []
    sub_1s = 0
    time_ms = 0
    for intent in intents:
        bought, _sold, _cost, buy_fees, _proceeds, sell_fees = position_legs(intent)
        if bought > 0:
            contracts += bought
            markets.add(intent.market_ticker)
        if intent in closed:
            fees += buy_fees + sell_fees
            reason = intent.exit_reason or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
        hold = _hold_ms(intent, now_ms)
        if hold is None:
            continue
        if intent.status == "open" or intent in closed:
            time_ms += hold
        if intent in closed:
            holds.append(hold)
            if hold < 1000:
                sub_1s += 1
    gross = net + fees
    drawdown_from_bankroll = bankroll - equity
    if drawdown_from_bankroll < 0:
        drawdown_from_bankroll = Decimal("0")
    return {
        "net_pnl": _num(net),
        "gross_pnl": _num(gross),
        "fees": _num(fees),
        "closes": len(closed),
        "contracts": _num(contracts),
        "unique_markets": len(markets),
        "max_open": max_open,
        "time_in_market_s": round(time_ms / 1000.0, 3),
        "avg_hold_ms": None if not holds else round(sum(holds) / len(holds), 3),
        "sub_1s_closes": sub_1s,
        "exit_reasons": reasons,
        "drawdown_from_bankroll": _num(drawdown_from_bankroll),
        "max_drawdown_peak": _num(max_drawdown),
        "equity_peak": _num(equity_peak),
        "equity": _num(equity),
        "learn_exit_orders": learn_exit_orders,
        "influences": learner_influences(learn_exit_orders=learn_exit_orders),
        "brier": brier_report(predictions),
        "activity_note": ACTIVITY_NOTE,
        "caveats": [FLIP_ENTRY_CAVEAT, ACTIVITY_NOTE],
    }


def _hold_ms(intent: PaperIntent, now_ms: int | None) -> int | None:
    if intent.exit_delta and intent.exit_delta.get("hold_ms") is not None:
        return int(intent.exit_delta["hold_ms"])
    if intent.opened_ms is None:
        return None
    if intent.status in ("closed", "settled"):
        last = next((fill.ts_ms for fill in reversed(intent.fills) if fill.ts_ms is not None), None)
        if last is not None and last >= intent.opened_ms:
            return last - intent.opened_ms
        return None
    if intent.status == "open" and now_ms is not None and now_ms >= intent.opened_ms:
        return now_ms - intent.opened_ms
    return None


def _market_prob(row: dict[str, Any]) -> float | None:
    mid = row.get("yes_mid")
    if mid is not None:
        return float(mid)
    ask = row.get("yes_ask")
    if ask is None:
        return None
    return float(ask)


def _brier(probs: list[float], labels: list[int]) -> float | None:
    if not probs or len(probs) != len(labels):
        return None
    total = 0.0
    for prob, label in zip(probs, labels, strict=True):
        total += (prob - label) ** 2
    return round(total / len(probs), 6)


def _feature_delta(entry: Any, exit_mark: Any) -> dict[str, float] | None:
    if not isinstance(entry, list) or not isinstance(exit_mark, list):
        return None
    out: dict[str, float] = {}
    for name, before, after in zip(FEATURE_NAMES, entry, exit_mark, strict=False):
        try:
            out[name] = round(float(after) - float(before), 6)
        except (TypeError, ValueError):
            continue
    return out


def _flags(quote: dict[str, Any] | None) -> dict[str, bool]:
    if not quote:
        return {}
    raw = quote.get("predicates")
    if not isinstance(raw, dict):
        return {}
    return {str(key): bool(value) for key, value in raw.items()}


def _get(quote: dict[str, Any] | None, key: str) -> Any:
    if quote is None:
        return None
    return quote.get(key)


def _sub(after: Any, before: Any) -> str | None:
    if after is None or before is None:
        return None
    try:
        return format(Decimal(str(after)) - Decimal(str(before)), "f")
    except (ArithmeticError, ValueError):
        return None


def _sub_float(after: Any, before: Any) -> float | None:
    if after is None or before is None:
        return None
    try:
        return round(float(after) - float(before), 6)
    except (TypeError, ValueError):
        return None


def _num(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return format(Decimal(str(value)), "f")
