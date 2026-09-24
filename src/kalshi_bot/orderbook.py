"""In-memory book rebuilt only from snapshot + deltas. Gaps are never filled in."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Never

Side = Literal["yes", "no"]


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying one orderbook message."""

    ok: bool
    applied: bool = False
    need_snapshot: bool = False
    invalid_reason: str | None = None


@dataclass
class OrderbookState:
    """YES/NO bid levels. Implied asks are 1 - opposite best bid."""

    market_ticker: str | None = None
    sid: int | None = None
    last_seq: int | None = None
    valid: bool = False
    yes: dict[Decimal, Decimal] = field(default_factory=dict)
    no: dict[Decimal, Decimal] = field(default_factory=dict)

    def reset(self) -> None:
        self.market_ticker = None
        self.sid = None
        self.last_seq = None
        self.valid = False
        self.yes.clear()
        self.no.clear()

    def best_bid(self, side: Side) -> Decimal | None:
        book = self._book(side)
        return max(book) if book else None

    def implied_ask(self, side: Side) -> Decimal | None:
        opposite: Side = "no" if side == "yes" else "yes"
        bid = self.best_bid(opposite)
        if bid is None:
            return None
        return Decimal("1") - bid

    def size_at(self, side: Side, price: Decimal) -> Decimal:
        return self._book(side).get(price, Decimal("0"))

    def bid_levels(self, side: Side) -> list[tuple[Decimal, Decimal]]:
        """Visible bids from best to worst as ``(price, size)``."""
        book = self._book(side)
        return [(price, book[price]) for price in sorted(book, reverse=True)]

    def ask_levels(self, side: Side) -> list[tuple[Decimal, Decimal]]:
        """Visible asks from best to worst as ``(price, size)``.

        Size lives on the opposite bid: a YES ask of ``1 - bid`` is the NO bid.
        """
        opposite: Side = "no" if side == "yes" else "yes"
        book = self._book(opposite)
        levels: list[tuple[Decimal, Decimal]] = []
        for bid in sorted(book, reverse=True):
            levels.append((Decimal("1") - bid, book[bid]))
        return levels

    def apply_snapshot(self, message: dict[str, Any]) -> ApplyResult:
        sid = message.get("sid")
        seq = message.get("seq")
        payload = message.get("msg") or {}
        ticker = payload.get("market_ticker")
        if sid is None or seq is None or not ticker:
            self._invalidate()
            return ApplyResult(
                ok=False,
                need_snapshot=True,
                invalid_reason="snapshot_missing_fields",
            )
        self.market_ticker = str(ticker)
        self.sid = int(sid)
        self.last_seq = int(seq)
        self.yes = _levels(payload.get("yes_dollars_fp"))
        self.no = _levels(payload.get("no_dollars_fp"))
        self.valid = True
        return ApplyResult(ok=True, applied=True)

    def apply_delta(self, message: dict[str, Any]) -> ApplyResult:
        if not self.valid:
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="book_invalid")
        sid = message.get("sid")
        seq = message.get("seq")
        payload = message.get("msg") or {}
        if sid is None or seq is None:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="delta_missing_fields")
        if int(sid) != self.sid:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="sid_mismatch")
        expected = (self.last_seq or 0) + 1
        incoming = int(seq)
        if incoming != expected:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="seq_gap")
        ticker = payload.get("market_ticker")
        if not ticker or str(ticker) != self.market_ticker:
            self._invalidate()
            reason = "ticker_mismatch" if ticker else "delta_missing_ticker"
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason=reason)
        side = payload.get("side")
        if side not in ("yes", "no"):
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_side")
        price = Decimal(str(payload["price_dollars"]))
        delta = Decimal(str(payload["delta_fp"]))
        book = self._book(side)
        nxt = book.get(price, Decimal("0")) + delta
        if nxt < 0:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="negative_level")
        if nxt == 0:
            book.pop(price, None)
        else:
            book[price] = nxt
        self.last_seq = incoming
        return ApplyResult(ok=True, applied=True)

    def _invalidate(self) -> None:
        """Drop quotes. Identity stays so a later snapshot can replace this book."""
        self.valid = False
        self.yes.clear()
        self.no.clear()

    def _book(self, side: Side) -> dict[Decimal, Decimal]:
        if side == "yes":
            return self.yes
        if side == "no":
            return self.no
        unreachable: Never = side
        raise ValueError(f"unknown side {unreachable!r}")


def _levels(rows: Any) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    if not rows:
        return out
    for row in rows:
        price = Decimal(str(row[0]))
        qty = Decimal(str(row[1]))
        if qty > 0:
            out[price] = qty
    return out
