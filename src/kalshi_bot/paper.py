"""Local paper fills on the live book/tape. No Kalshi order is sent.

A long YES or NO is closed by selling that same side into the visible bid
(docs: sell-yes / sell-no; buying the other outcome does not flatten). The
book stores bids only, so a YES bid hit pays the YES bid and a taker fee on
that price. Maker entries still rest and fill from the public tape. Exits
take the bid they can see; there is no queue position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Never

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState, Side

PaperStyle = Literal["maker", "taker"]
PaperAction = Literal["buy", "sell"]
PaperStatus = Literal["open", "settled", "closed", "cancelled"]
SettlementSource = Literal["official", "reconstructed"]
_EXIT_SLIPPAGE = Decimal("0.02")


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
    action: PaperAction = "buy"


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
    exit_reason: str | None = None
    entry_features: list[float] = field(default_factory=list)
    opened_ms: int | None = None

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

    def sell_open(
        self,
        intent: PaperIntent,
        book: OrderbookState,
        *,
        ts_ms: int | None,
        min_price: Decimal,
        reason: str,
        slippage: Decimal = _EXIT_SLIPPAGE,
    ) -> list[PaperFill]:
        """Hit bids on the held side, up to two cents through the touch."""
        if intent.status != "open" or not book.valid or book.market_ticker != intent.market_ticker:
            return []
        left = net_open_qty(intent)
        if left <= 0:
            return []
        levels = book.bid_levels(intent.outcome)
        if not levels:
            return []
        floor = max(min_price, levels[0][0] - slippage)
        fills: list[PaperFill] = []
        for bid, available in levels:
            if left <= 0 or bid < floor:
                break
            room = self._room(intent.market_ticker, intent.outcome, bid, available)
            qty = min(left, room)
            if qty <= 0:
                continue
            self._consume(intent.market_ticker, intent.outcome, bid, qty)
            fills.extend(
                self._fill(
                    intent,
                    price=bid,
                    count=qty,
                    ts_ms=ts_ms,
                    source="book",
                    action="sell",
                    fee_style="taker",
                )
            )
            left -= qty
        if net_open_qty(intent) <= 0 and intent.remaining <= 0 and position_legs(intent)[0] > 0:
            _mark_closed(intent, reason)
        return fills

    def cancel_resting(self, intent: PaperIntent, reason: str) -> bool:
        """Drop unfilled size. A filled position stays open until it is sold or settled."""
        if intent.status != "open" or intent.remaining <= 0:
            return False
        intent.remaining = Decimal("0")
        if net_open_qty(intent) <= 0 and not intent.fills:
            intent.status = "cancelled"
            intent.exit_reason = reason
            intent.pnl = Decimal("0")
            intent.won = None
        return True

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
            intent.status = "settled"
            intent.settled = True
            intent.remaining = Decimal("0")
            intent.won = yes_won if intent.outcome == "yes" else (not yes_won)
            intent.exit_reason = intent.exit_reason or "settlement"
            bought, sold, buy_cost, buy_fees, sell_proceeds, sell_fees = position_legs(intent)
            open_qty = bought - sold
            if open_qty < 0:
                open_qty = Decimal("0")
            if bought <= 0:
                intent.pnl = Decimal("0")
                continue
            payout = open_qty if intent.won else Decimal("0")
            intent.pnl = payout + sell_proceeds - sell_fees - buy_cost - buy_fees
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
        action: PaperAction = "buy",
        fee_style: PaperStyle | None = None,
    ) -> list[PaperFill]:
        style = fee_style if fee_style is not None else intent.style
        fee = Decimal("0")
        if style == "taker":
            fee = quadratic_taker_fee(count, price, multiplier=self.fee_multiplier)
        elif style == "maker":
            fee = Decimal("0")
        else:
            unreachable: Never = style
            raise ValueError(f"unknown style {unreachable!r}")
        fill = PaperFill(
            market_ticker=intent.market_ticker,
            style=style,
            outcome=intent.outcome,
            price=price,
            count=count,
            fee=fee,
            ts_ms=ts_ms,
            source=source,
            action=action,
        )
        if action == "buy":
            intent.remaining -= count
            if intent.opened_ms is None:
                intent.opened_ms = ts_ms
        elif action == "sell":
            pass
        else:
            unreachable_action: Never = action
            raise ValueError(f"unknown action {unreachable_action!r}")
        intent.fills.append(fill)
        return [fill]


def _is_open(intent: PaperIntent) -> bool:
    return intent.status == "open" and not intent.settled and intent.remaining > 0


def position_legs(
    intent: PaperIntent,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Bought qty, sold qty, buy cost, buy fees, sell proceeds, sell fees."""
    bought = Decimal("0")
    sold = Decimal("0")
    buy_cost = Decimal("0")
    buy_fees = Decimal("0")
    sell_proceeds = Decimal("0")
    sell_fees = Decimal("0")
    for fill in intent.fills:
        if fill.action == "buy":
            bought += fill.count
            buy_cost += fill.price * fill.count
            buy_fees += fill.fee
        elif fill.action == "sell":
            sold += fill.count
            sell_proceeds += fill.price * fill.count
            sell_fees += fill.fee
        else:
            unreachable: Never = fill.action
            raise ValueError(f"unknown action {unreachable!r}")
    return bought, sold, buy_cost, buy_fees, sell_proceeds, sell_fees


def net_open_qty(intent: PaperIntent) -> Decimal:
    bought, sold, *_rest = position_legs(intent)
    qty = bought - sold
    if qty < 0:
        return Decimal("0")
    return qty


def open_cost(intent: PaperIntent) -> Decimal:
    """Entry premium still tied to contracts that have not been sold."""
    bought, sold, buy_cost, _buy_fees, _proceeds, _sell_fees = position_legs(intent)
    if bought <= 0:
        return Decimal("0")
    qty = bought - sold
    if qty <= 0:
        return Decimal("0")
    return buy_cost * qty / bought


def open_cost_with_fees(intent: PaperIntent) -> Decimal:
    bought, sold, buy_cost, buy_fees, _proceeds, _sell_fees = position_legs(intent)
    if bought <= 0:
        return Decimal("0")
    qty = bought - sold
    if qty <= 0:
        return Decimal("0")
    return (buy_cost + buy_fees) * qty / bought


def _mark_closed(intent: PaperIntent, reason: str) -> None:
    _bought, _sold, buy_cost, buy_fees, sell_proceeds, sell_fees = position_legs(intent)
    intent.status = "closed"
    intent.settled = False
    intent.remaining = Decimal("0")
    intent.exit_reason = reason
    intent.pnl = sell_proceeds - sell_fees - buy_cost - buy_fees
    if intent.pnl > 0:
        intent.won = True
    elif intent.pnl < 0:
        intent.won = False
    else:
        intent.won = None


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
