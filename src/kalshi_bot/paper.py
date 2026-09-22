"""Local paper fills on the live book/tape. No Kalshi order is sent."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Never

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState, Side

PaperStyle = Literal["maker", "taker"]


@dataclass
class PaperFill:
    """One local fill against the production stream."""

    market_ticker: str
    style: PaperStyle
    outcome: Side
    price: Decimal
    count: Decimal
    fee: Decimal
    ts_ms: int | None
    source: str


@dataclass
class PaperIntent:
    """A resting or aggressive paper order that never leaves this process."""

    market_ticker: str
    outcome: Side
    price: Decimal
    count: Decimal
    style: PaperStyle
    remaining: Decimal = field(init=False)
    fills: list[PaperFill] = field(default_factory=list)
    settled: bool = False
    won: bool | None = None
    pnl: Decimal | None = None

    def __post_init__(self) -> None:
        self.remaining = self.count


@dataclass
class PaperLedger:
    """Applies stream events to paper intents."""

    fee_multiplier: Decimal = Decimal("1")
    intents: list[PaperIntent] = field(default_factory=list)

    def add(self, intent: PaperIntent) -> None:
        self.intents.append(intent)

    def on_book(self, book: OrderbookState, *, ts_ms: int | None) -> list[PaperFill]:
        fills: list[PaperFill] = []
        if not book.valid or not book.market_ticker:
            return fills
        for intent in self.intents:
            if intent.style != "taker" or intent.remaining <= 0:
                continue
            if intent.market_ticker != book.market_ticker:
                continue
            ask = book.implied_ask(intent.outcome)
            if ask is None or ask > intent.price:
                continue
            opposite: Side = "no" if intent.outcome == "yes" else "yes"
            available = book.size_at(opposite, Decimal("1") - ask)
            qty = min(intent.remaining, available)
            if qty <= 0:
                continue
            fills.extend(self._fill(intent, price=ask, count=qty, ts_ms=ts_ms, source="book"))
        return fills

    def on_trade(self, message: dict[str, Any]) -> list[PaperFill]:
        payload = message.get("msg") or {}
        ticker = payload.get("market_ticker")
        if not ticker:
            return []
        yes_price = Decimal(str(payload["yes_price_dollars"]))
        count = Decimal(str(payload["count_fp"]))
        ts_ms = payload.get("ts_ms")
        taker_book_side = payload.get("taker_book_side")
        fills: list[PaperFill] = []
        for intent in self.intents:
            if intent.style != "maker" or intent.remaining <= 0:
                continue
            if intent.market_ticker != ticker:
                continue
            if not _maker_hit(intent.outcome, taker_book_side):
                continue
            trade_price = yes_price if intent.outcome == "yes" else (Decimal("1") - yes_price)
            if trade_price > intent.price:
                continue
            qty = min(intent.remaining, count)
            if qty <= 0:
                continue
            fills.extend(
                self._fill(
                    intent,
                    price=trade_price,
                    count=qty,
                    ts_ms=int(ts_ms) if ts_ms is not None else None,
                    source="trade",
                )
            )
        return fills

    def on_settlement(
        self,
        *,
        market_ticker: str,
        close_avg: Decimal,
        floor_strike: Decimal,
    ) -> None:
        yes_won = close_avg >= floor_strike
        for intent in self.intents:
            if intent.market_ticker != market_ticker or intent.settled:
                continue
            filled = intent.count - intent.remaining
            intent.settled = True
            intent.won = yes_won if intent.outcome == "yes" else (not yes_won)
            if filled <= 0:
                intent.pnl = Decimal("0")
                continue
            fees = sum((fill.fee for fill in intent.fills), Decimal("0"))
            cost = sum((fill.price * fill.count for fill in intent.fills), Decimal("0"))
            payout = filled if intent.won else Decimal("0")
            intent.pnl = payout - cost - fees

    def _fill(
        self,
        intent: PaperIntent,
        *,
        price: Decimal,
        count: Decimal,
        ts_ms: int | None,
        source: str,
    ) -> list[PaperFill]:
        fee = Decimal("0")
        if intent.style == "taker":
            fee = quadratic_taker_fee(count, price, multiplier=self.fee_multiplier)
        elif intent.style == "maker":
            fee = Decimal("0")
        else:
            unreachable: Never = intent.style
            raise ValueError(f"unknown style {unreachable!r}")
        fill = PaperFill(
            market_ticker=intent.market_ticker,
            style=intent.style,
            outcome=intent.outcome,
            price=price,
            count=count,
            fee=fee,
            ts_ms=ts_ms,
            source=source,
        )
        intent.remaining -= count
        intent.fills.append(fill)
        return [fill]


def _maker_hit(outcome: Side, taker_book_side: Any) -> bool:
    """A YES bid is hit when the taker lifts the ask-equivalent (NO) book side."""
    if outcome == "yes":
        return taker_book_side == "ask"
    if outcome == "no":
        return taker_book_side == "bid"
    unreachable: Never = outcome
    raise ValueError(f"unknown outcome {unreachable!r}")
