"""Online logistic learner for the paper session. No network calls.

Weights start at zero, so the score is 0.5 until enough closed trades exist.
Before that the signal-edge rules run alone. State is stored on the session
so a restart keeps learning. A new session file starts cold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kalshi_bot.strategy import StrategyParams

FEATURE_NAMES: tuple[str, ...] = (
    "edge",
    "last_minute",
    "time_left",
    "imbalance",
    "spread",
    "depth",
    "maker",
    "price_vs_model",
)
N_FEATURES = len(FEATURE_NAMES)
MIN_INFLUENCE = 4
_EXIT_PROB = 0.40
_HARD_PROB = 0.40
_MILD_PROB = 0.48
_LR = 0.25
_L2 = 0.20
_EWMA = 0.30


@dataclass
class OnlineLearner:
    """One SGD step per closed trade. L2 keeps a short sample from dominating."""

    weights: list[float]
    bias: float = 0.0
    n: int = 0
    last_reason: str = "cold start"
    maker_ewma: float = 0.0
    taker_ewma: float = 0.0
    maker_n: int = 0
    taker_n: int = 0

    @classmethod
    def cold(cls) -> OnlineLearner:
        return cls(weights=[0.0] * N_FEATURES)

    def predict(self, features: list[float]) -> float:
        if len(features) != N_FEATURES:
            return 0.5
        score = self.bias
        for weight, value in zip(self.weights, features, strict=True):
            score += weight * value
        return _sigmoid(score)

    def update(
        self,
        features: list[float],
        *,
        won: bool,
        pnl: Decimal,
        style: str,
        reason: str,
    ) -> None:
        if len(features) != N_FEATURES:
            return
        target = 1.0 if won else 0.0
        probability = self.predict(features)
        error = probability - target
        self.bias -= _LR * (error + _L2 * self.bias)
        self.weights = [
            weight - _LR * (error * value + _L2 * weight)
            for weight, value in zip(self.weights, features, strict=True)
        ]
        self._note_style(style, float(pnl))
        self.n += 1
        self.last_reason = f"{reason} {'win' if won else 'loss'} pnl {format(pnl, 'f')}"

    def wants_exit(self, features: list[float]) -> bool:
        """True when this live setup resembles trades that lost."""
        if self.n < MIN_INFLUENCE or len(features) != N_FEATURES:
            return False
        return self.predict(features) < _EXIT_PROB

    def adjust_params(self, params: StrategyParams, features: list[float]) -> StrategyParams:
        """Tighten entry only. Never raises size above the rule-based params."""
        if self.n < MIN_INFLUENCE or len(features) != N_FEATURES:
            return params
        probability = self.predict(features)
        extra = Decimal("0")
        contracts = params.contracts
        if probability < _HARD_PROB:
            extra = Decimal("0.02")
            contracts = Decimal("1")
        elif probability < _MILD_PROB:
            extra = Decimal("0.01")
            contracts = max(Decimal("1"), params.contracts - 1)
        maker = _clamp_bias(params.maker_bias + self._maker_delta())
        contracts = min(params.contracts, max(Decimal("1"), contracts))
        tilted = StrategyParams(
            mid_edge=min(Decimal("0.20"), params.mid_edge + extra),
            last_minute_edge=min(Decimal("0.12"), params.last_minute_edge + extra),
            contracts=contracts,
            maker_bias=maker,
            cooldown_s=params.cooldown_s,
            max_open_risk=params.max_open_risk,
            risk_ceiling=params.risk_ceiling,
            name=params.name,
            stop_loss=params.stop_loss,
            take_profit=params.take_profit,
            flip_margin=params.flip_margin,
        )
        if tilted == params:
            return params
        return tilted

    def summary(self) -> dict[str, Any]:
        weights = {
            name: round(value, 4)
            for name, value in zip(FEATURE_NAMES, self.weights, strict=True)
        }
        return {
            "samples": self.n,
            "active": self.n >= MIN_INFLUENCE,
            "min_samples": MIN_INFLUENCE,
            "last_reason": self.last_reason,
            "weights": weights,
            "maker_n": self.maker_n,
            "taker_n": self.taker_n,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": [round(value, 8) for value in self.weights],
            "bias": round(self.bias, 8),
            "n": self.n,
            "last_reason": self.last_reason,
            "maker_ewma": round(self.maker_ewma, 8),
            "taker_ewma": round(self.taker_ewma, 8),
            "maker_n": self.maker_n,
            "taker_n": self.taker_n,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> OnlineLearner:
        if not isinstance(raw, dict):
            return cls.cold()
        weights = raw.get("weights")
        if not isinstance(weights, list) or len(weights) != N_FEATURES:
            return cls.cold()
        try:
            parsed = [float(value) for value in weights]
        except (TypeError, ValueError):
            return cls.cold()
        return cls(
            weights=parsed,
            bias=float(raw.get("bias") or 0.0),
            n=int(raw.get("n") or 0),
            last_reason=str(raw.get("last_reason") or "cold start"),
            maker_ewma=float(raw.get("maker_ewma") or 0.0),
            taker_ewma=float(raw.get("taker_ewma") or 0.0),
            maker_n=int(raw.get("maker_n") or 0),
            taker_n=int(raw.get("taker_n") or 0),
        )

    def _note_style(self, style: str, pnl: float) -> None:
        if style == "maker":
            self.maker_n += 1
            self.maker_ewma = _ewma(self.maker_ewma, pnl, self.maker_n)
            return
        if style == "taker":
            self.taker_n += 1
            self.taker_ewma = _ewma(self.taker_ewma, pnl, self.taker_n)
            return

    def _maker_delta(self) -> Decimal:
        if self.maker_n < 2 or self.taker_n < 2:
            return Decimal("0")
        if self.maker_ewma + 0.02 < self.taker_ewma:
            return Decimal("-0.20")
        if self.taker_ewma + 0.02 < self.maker_ewma:
            return Decimal("0.05")
        return Decimal("0")


def _ewma(previous: float, sample: float, n: int) -> float:
    if n <= 1:
        return sample
    return (1.0 - _EWMA) * previous + _EWMA * sample


def _clamp_bias(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def _sigmoid(score: float) -> float:
    if score > 30.0:
        return 1.0
    if score < -30.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-score))
