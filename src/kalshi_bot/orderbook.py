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

    def apply_snapshot(self, message: dict[str, Any]) -> ApplyResult:
        sid = message.get("sid")
        seq = message.get("seq")
        payload = message.get("msg") or {}
        ticker = payload.get("market_ticker")
        if sid is None or seq is None or not ticker:
            return ApplyResult(ok=False, invalid_reason="snapshot_missing_fields")
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
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="delta_missing_fields")
        if int(sid) != self.sid:
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="sid_mismatch")
        expected = (self.last_seq or 0) + 1
        incoming = int(seq)
        if incoming != expected:
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="seq_gap")
        side = payload.get("side")
        if side not in ("yes", "no"):
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_side")
        price = Decimal(str(payload["price_dollars"]))
        delta = Decimal(str(payload["delta_fp"]))
        book = self._book(side)
        nxt = book.get(price, Decimal("0")) + delta
        if nxt < 0:
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="negative_level")
        if nxt == 0:
            book.pop(price, None)
        else:
            book[price] = nxt
        self.last_seq = incoming
        ticker = payload.get("market_ticker")
        if ticker:
            self.market_ticker = str(ticker)
        return ApplyResult(ok=True, applied=True)

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
