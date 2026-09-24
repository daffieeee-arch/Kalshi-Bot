"""Local paper fills on the live book/tape. No Kalshi order is sent."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Never

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState, Side

PaperStyle = Literal["maker", "taker"]
PaperStatus = Literal["open", "settled"]
SettlementSource = Literal["official", "reconstructed"]


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


@dataclass(frozen=True)
class SettlementMark:
    """Realized outcome for one market. Official result wins over a local compare."""

    market_ticker: str
    yes_won: bool
    source: SettlementSource
    reconstructed_yes: bool | None
    mismatch: bool


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
    status: PaperStatus = "open"
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
    # Displayed size already used. The captured book itself is not edited.
    _taken: dict[tuple[str, Side, Decimal], Decimal] = field(default_factory=dict)

    def add(self, intent: PaperIntent) -> None:
        self.intents.append(intent)

    def on_book(self, book: OrderbookState, *, ts_ms: int | None) -> list[PaperFill]:
        fills: list[PaperFill] = []
        if not book.valid or not book.market_ticker:
            return fills
        for intent in self.intents:
            if not _is_open(intent) or intent.style != "taker":
                continue
            if intent.market_ticker != book.market_ticker:
                continue
            opposite: Side = "no" if intent.outcome == "yes" else "yes"
            for ask, available in book.ask_levels(intent.outcome):
                if intent.remaining <= 0 or ask > intent.price:
                    break
                price_level = Decimal("1") - ask
                room = self._room(intent.market_ticker, opposite, price_level, available)
                qty = min(intent.remaining, room)
                if qty <= 0:
                    continue
                self._consume(intent.market_ticker, opposite, price_level, qty)
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
        left = count
        for intent in self.intents:
            if not _is_open(intent) or intent.style != "maker":
                continue
            if intent.market_ticker != ticker:
                continue
            if not _maker_hit(intent.outcome, taker_book_side):
                continue
            trade_price = yes_price if intent.outcome == "yes" else (Decimal("1") - yes_price)
            if trade_price > intent.price:
                continue
            qty = min(intent.remaining, left)
            if qty <= 0:
                continue
            left -= qty
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
        close_avg: Decimal | None = None,
        floor_strike: Decimal | None = None,
        official_result: Literal["yes", "no"] | None = None,
    ) -> SettlementMark | None:
        """Settle open intents. `official_result` is the P&L; the average is QA only."""
        reconstructed = _reconstructed_yes(close_avg, floor_strike)
        if official_result == "yes":
            yes_won = True
            source: SettlementSource = "official"
        elif official_result == "no":
            yes_won = False
            source = "official"
        elif official_result is None:
            if reconstructed is None:
                return None
            yes_won = reconstructed
            source = "reconstructed"
        else:
            unreachable: Never = official_result
            raise ValueError(f"unknown official result {unreachable!r}")
        mismatch = reconstructed is not None and reconstructed != yes_won
        for intent in self.intents:
            if intent.market_ticker != market_ticker or intent.status != "open":
                continue
            filled = sum((fill.count for fill in intent.fills), Decimal("0"))
            intent.status = "settled"
            intent.settled = True
            intent.remaining = Decimal("0")
            intent.won = yes_won if intent.outcome == "yes" else (not yes_won)
            if filled <= 0:
                intent.pnl = Decimal("0")
                continue
            fees = sum((fill.fee for fill in intent.fills), Decimal("0"))
            cost = sum((fill.price * fill.count for fill in intent.fills), Decimal("0"))
            payout = filled if intent.won else Decimal("0")
            intent.pnl = payout - cost - fees
        return SettlementMark(
            market_ticker=market_ticker,
            yes_won=yes_won,
            source=source,
            reconstructed_yes=reconstructed,
            mismatch=mismatch,
        )

    def export_taken(self) -> list[tuple[str, Side, Decimal, Decimal]]:
        """Displayed size already used, so a reload cannot take it again."""
        return [(ticker, side, price, qty) for (ticker, side, price), qty in self._taken.items()]

    def import_taken(self, rows: list[tuple[str, Side, Decimal, Decimal]]) -> None:
        self._taken = {(ticker, side, price): qty for ticker, side, price, qty in rows}

    def _room(self, ticker: str, side: Side, price: Decimal, available: Decimal) -> Decimal:
        key = (ticker, side, price)
        taken = self._taken.get(key, Decimal("0"))
        if available < taken:
            taken = available
            self._taken[key] = taken
        return available - taken

    def _consume(self, ticker: str, side: Side, price: Decimal, qty: Decimal) -> None:
        key = (ticker, side, price)
        self._taken[key] = self._taken.get(key, Decimal("0")) + qty

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


def _is_open(intent: PaperIntent) -> bool:
    return intent.status == "open" and not intent.settled and intent.remaining > 0


def _reconstructed_yes(close_avg: Decimal | None, floor_strike: Decimal | None) -> bool | None:
    """QA compare of the published BRTI window average to the floor strike.

    The feed already formats that average to 8 decimal places. Kalshi's market
    `result` is the realized outcome; this compare is not a second settlement.
    """
    if close_avg is None or floor_strike is None:
        return None
    return close_avg >= floor_strike


def _maker_hit(outcome: Side, taker_book_side: Any) -> bool:
    """A YES bid is hit when the taker lifts the ask-equivalent (NO) book side."""
    if outcome == "yes":
        return taker_book_side == "ask"
    if outcome == "no":
        return taker_book_side == "bid"
    unreachable: Never = outcome
    raise ValueError(f"unknown outcome {unreachable!r}")
