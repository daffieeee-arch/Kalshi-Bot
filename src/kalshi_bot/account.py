"""Local cash ledger around ``PaperLedger``. No exchange balance is touched.

Binary settlement pays $1 per winning contract and $0 on a loss. Kalshi charges
no settlement fee for a simple yes/no result. Cost and taker fees leave cash
at fill time, so settlement only adds the payout.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Never

from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.paper import PaperFill, PaperIntent, PaperLedger


class PaperAccount:
    """Fake dollars. ``cash`` is settled money; open marks sit on top."""

    def __init__(self, bankroll: Decimal, ledger: PaperLedger | None = None) -> None:
        if bankroll <= 0:
            raise ValueError("bankroll must be positive")
        self.bankroll = bankroll
        self.cash = bankroll
        self.ledger = ledger or PaperLedger()
        self._credited: set[int] = set()

    def note_fills(self, fills: list[PaperFill]) -> None:
        for fill in fills:
            self.cash -= fill.price * fill.count + fill.fee

    def mark_credited(self) -> None:
        """Settled intents already included in ``cash`` (after a reload)."""
        for intent in self.ledger.intents:
            if intent.status == "settled":
                self._credited.add(id(intent))

    def credit_settlements(self) -> Decimal:
        """Pay $1 per winning contract once. Returns the dollars just credited."""
        paid = Decimal("0")
        for intent in self.ledger.intents:
            key = id(intent)
            if intent.status != "settled" or key in self._credited:
                continue
            self._credited.add(key)
            if not intent.won:
                continue
            filled = sum((fill.count for fill in intent.fills), Decimal("0"))
            if filled <= 0:
                continue
            payout = filled
            self.cash += payout
            paid += payout
        return paid

    def open_risk(self) -> Decimal:
        """Unsettled fill cost plus resting notional still able to fill."""
        risk = Decimal("0")
        for intent in self.ledger.intents:
            if intent.status != "open":
                continue
            for fill in intent.fills:
                risk += fill.price * fill.count + fill.fee
            if intent.remaining > 0:
                risk += intent.remaining * _unit_reserve(intent)
        return risk

    def available_cash(self) -> Decimal:
        reserved = Decimal("0")
        for intent in self.ledger.intents:
            if intent.status != "open" or intent.remaining <= 0:
                continue
            reserved += intent.remaining * _unit_reserve(intent)
        return self.cash - reserved

    def mark_value(self, book: OrderbookState) -> Decimal:
        """Open contracts at the bid, or at cost when that book is not live."""
        total = Decimal("0")
        for intent in self.ledger.intents:
            if intent.status != "open":
                continue
            filled = sum((fill.count for fill in intent.fills), Decimal("0"))
            if filled <= 0:
                continue
            bid = _mark_bid(book, intent)
            if bid is None:
                total += sum((fill.price * fill.count for fill in intent.fills), Decimal("0"))
            else:
                total += bid * filled
        return total

    def equity(self, book: OrderbookState) -> Decimal:
        return self.cash + self.mark_value(book)

    def realized_pnl(self) -> Decimal:
        return sum(
            (intent.pnl or Decimal("0") for intent in self.ledger.intents if intent.status == "settled"),
            Decimal("0"),
        )

    def unrealized_pnl(self, book: OrderbookState) -> Decimal:
        return self.equity(book) - self.bankroll - self.realized_pnl()

    def win_loss(self) -> tuple[int, int]:
        wins = 0
        losses = 0
        for intent in self.ledger.intents:
            if intent.status != "settled" or not intent.fills or intent.pnl is None:
                continue
            if intent.pnl > 0:
                wins += 1
            elif intent.pnl < 0:
                losses += 1
        return wins, losses


def _unit_reserve(intent: PaperIntent) -> Decimal:
    if intent.style == "taker":
        return intent.price + quadratic_taker_fee(Decimal("1"), intent.price)
    if intent.style == "maker":
        return intent.price
    unreachable: Never = intent.style
    raise ValueError(f"unknown style {unreachable!r}")


def _mark_bid(book: OrderbookState, intent: PaperIntent) -> Decimal | None:
    if not book.valid or book.market_ticker != intent.market_ticker:
        return None
    return book.best_bid(intent.outcome)
