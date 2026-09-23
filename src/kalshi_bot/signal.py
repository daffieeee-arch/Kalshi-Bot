"""Local YES probability vs the Kalshi book. Paper only; not an edge claim."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from kalshi_bot.fees import quadratic_taker_fee

Regime = Literal["mid", "last_minute"]

# Fallback BTC dollar-vol: ~$200 over 15 minutes → ~$6.7 per sqrt(second).
_DEFAULT_SIGMA = Decimal("6.7")
_LAST_MINUTE_S = 60
_CLOSE_TICKS = 60


@dataclass(frozen=True)
class Signal:
    """One local mark of model P(YES) against the live book."""

    regime: Regime
    spot: Decimal | None
    strike: Decimal | None
    gap: Decimal | None
    seconds_left: float | None
    sigma: Decimal
    model_yes: Decimal | None
    yes_ask: Decimal | None
    no_ask: Decimal | None
    yes_edge: Decimal | None
    no_edge: Decimal | None
    hint: str
    note: str


def norm_cdf(x: float) -> float:
    """Φ(x) without scipy."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def midwindow_yes_prob(
    spot: Decimal,
    strike: Decimal,
    seconds_left: float,
    sigma_per_sqrt_sec: Decimal,
) -> Decimal:
    """P(S_T ≥ K) if remaining BRTI is a random walk. Last-minute average ≈ S_T."""
    if seconds_left <= 0:
        return Decimal("1") if spot >= strike else Decimal("0")
    sigma = float(sigma_per_sqrt_sec)
    if sigma <= 0:
        return Decimal("1") if spot >= strike else Decimal("0")
    z = float(spot - strike) / (sigma * math.sqrt(seconds_left))
    return _clamp01(Decimal(str(norm_cdf(z))))


def last_minute_yes_prob(
    close_avg: Decimal,
    window_size: int,
    spot: Decimal,
    strike: Decimal,
    sigma_per_tick: Decimal,
) -> Decimal:
    """P(final 60-tick average ≥ K | n ticks locked at close_avg)."""
    n = max(0, min(int(window_size), _CLOSE_TICKS))
    if n >= _CLOSE_TICKS:
        return Decimal("1") if close_avg >= strike else Decimal("0")
    remaining = _CLOSE_TICKS - n
    locked = close_avg * n
    need_sum = (strike * _CLOSE_TICKS) - locked
    need_avg = need_sum / remaining
    sigma = float(sigma_per_tick)
    if sigma <= 0:
        return Decimal("1") if spot >= need_avg else Decimal("0")
    # Future levels are S_k = S_0 + e_1 + ... + e_k with independent N(0, sigma^2)
    # increments. SD of their average is not the SD of the last level.
    z = float(spot - need_avg) / (sigma * _mean_level_scale(remaining))
    return _clamp01(Decimal(str(norm_cdf(z))))


def realized_sigma(
    spots: list[Decimal],
    *,
    fallback: Decimal = _DEFAULT_SIGMA,
    timestamps_ms: list[int] | None = None,
) -> Decimal:
    """Stdev of successive BRTI diffs.

    Without timestamps each step is one tick. With timestamps, scale each
    increment by 1/sqrt(dt_seconds) so the result is dollars per sqrt(second).
    """
    if timestamps_ms is not None and len(timestamps_ms) != len(spots):
        raise ValueError("timestamps_ms must have one entry per spot")
    if len(spots) < 8:
        return fallback
    diffs: list[float] = []
    for i in range(1, len(spots)):
        step = float(spots[i] - spots[i - 1])
        if timestamps_ms is None:
            diffs.append(step)
            continue
        dt_ms = timestamps_ms[i] - timestamps_ms[i - 1]
        if dt_ms <= 0:
            continue
        diffs.append(step / math.sqrt(dt_ms / 1000.0))
    if len(diffs) < 7:
        return fallback
    mean = sum(diffs) / len(diffs)
    var = sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1)
    sigma = math.sqrt(max(var, 0.0))
    if sigma <= 0.5:
        return fallback
    return Decimal(str(round(sigma, 4)))


def sigma_from_samples(samples: list[tuple[int | None, Decimal]]) -> Decimal:
    """Per-sqrt-second sigma when every sample has a timestamp, else per tick."""
    values = [spot for _, spot in samples]
    stamps: list[int] = []
    for ts, _spot in samples:
        if ts is None:
            return realized_sigma(values)
        stamps.append(ts)
    if not stamps:
        return realized_sigma(values)
    return realized_sigma(values, timestamps_ms=stamps)


def evaluate(
    *,
    spot: Decimal | None,
    strike: Decimal | None,
    seconds_left: float | None,
    close_avg: Decimal | None,
    close_window: int | None,
    yes_ask: Decimal | None,
    no_ask: Decimal | None,
    sigma: Decimal,
    book_valid: bool = True,
    data_fresh: bool = True,
    market_status: str | None = None,
    book_ticker: str | None = None,
    reference_ticker: str | None = None,
) -> Signal:
    """Pick last-minute remaining-average math in the close minute, else mid-window."""
    gap = (spot - strike) if spot is not None and strike is not None else None
    in_last = (
        seconds_left is not None
        and 0 < seconds_left <= _LAST_MINUTE_S
        and close_avg is not None
        and close_window is not None
        and close_window > 0
    )
    regime: Regime = "last_minute" if in_last else "mid"
    model: Decimal | None = None
    note = "need BRTI and strike"
    if spot is not None and strike is not None and seconds_left is not None:
        if regime == "last_minute":
            assert close_avg is not None and close_window is not None
            model = last_minute_yes_prob(close_avg, close_window, spot, strike, sigma)
            note = f"last minute n={close_window}/60"
        else:
            model = midwindow_yes_prob(spot, strike, seconds_left, sigma)
            note = f"mid-window τ={seconds_left:.0f}s σ={sigma}"
    yes_edge = _edge(model, yes_ask) if model is not None and yes_ask is not None else None
    no_model = (Decimal("1") - model) if model is not None else None
    no_edge = _edge(no_model, no_ask) if no_model is not None and no_ask is not None else None
    eligible, blocked = _entry_block(
        seconds_left=seconds_left,
        book_valid=book_valid,
        data_fresh=data_fresh,
        market_status=market_status,
        book_ticker=book_ticker,
        reference_ticker=reference_ticker,
    )
    if not eligible:
        note = blocked
    return Signal(
        regime=regime,
        spot=spot,
        strike=strike,
        gap=gap,
        seconds_left=seconds_left,
        sigma=sigma,
        model_yes=model,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_edge=yes_edge,
        no_edge=no_edge,
        hint=_hint(regime, yes_edge, no_edge) if eligible else "wait",
        note=note,
    )


def signal_to_dict(signal: Signal) -> dict[str, str | float | None]:
    return {
        "regime": signal.regime,
        "spot": _fmt(signal.spot),
        "strike": _fmt(signal.strike),
        "gap": _fmt(signal.gap),
        "seconds_left": None if signal.seconds_left is None else round(signal.seconds_left, 1),
        "sigma": _fmt(signal.sigma),
        "model_yes": _fmt(signal.model_yes, places="0.0001"),
        "yes_ask": _fmt(signal.yes_ask),
        "no_ask": _fmt(signal.no_ask),
        "yes_edge": _fmt(signal.yes_edge, places="0.0001"),
        "no_edge": _fmt(signal.no_edge, places="0.0001"),
        "hint": signal.hint,
        "note": signal.note,
    }


_OPEN_STATUS = frozenset({"open", "active"})


def _mean_level_scale(remaining: int) -> float:
    """sqrt((r+1)*(2r+1)/(6r)) for the SD of a random-walk average."""
    r = remaining
    return math.sqrt((r + 1) * (2 * r + 1) / (6 * r))


def _entry_block(
    *,
    seconds_left: float | None,
    book_valid: bool,
    data_fresh: bool,
    market_status: str | None,
    book_ticker: str | None,
    reference_ticker: str | None,
) -> tuple[bool, str]:
    """Shared gate for paper hints. A closed, stale, or foreign book cannot hint."""
    if seconds_left is not None and seconds_left <= 0:
        return False, "market closed"
    if market_status is not None and market_status.lower() not in _OPEN_STATUS:
        return False, "market not open"
    if book_ticker and reference_ticker and book_ticker != reference_ticker:
        return False, "ticker mismatch"
    if not book_valid:
        return False, "book invalid"
    if not data_fresh:
        return False, "stale data"
    return True, ""


def _edge(model: Decimal, ask: Decimal) -> Decimal:
    fee = quadratic_taker_fee(Decimal("1"), ask)
    return model - ask - fee


def _hint(regime: Regime, yes_edge: Decimal | None, no_edge: Decimal | None) -> str:
    """Conservative paper hint. Mid-window needs a fatter gap than the close minute."""
    bar = Decimal("0.03") if regime == "last_minute" else Decimal("0.08")
    yes = yes_edge or Decimal("-1")
    no = no_edge or Decimal("-1")
    if yes >= bar and yes >= no:
        return "paper YES?"
    if no >= bar:
        return "paper NO?"
    return "wait"


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def _fmt(value: Decimal | None, *, places: str | None = None) -> str | None:
    if value is None:
        return None
    if places is not None:
        value = value.quantize(Decimal(places))
    return format(value, "f")
