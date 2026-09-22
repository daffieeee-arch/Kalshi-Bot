"""In-memory KXBTC15M order book rebuilt only from snapshot + deltas."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying one orderbook message. Gaps are never filled in."""

    ok: bool
    applied: bool = False
    need_snapshot: bool = False
    invalid_reason: str | None = None


def health_from_apply(
    result: ApplyResult,
    *,
    seq: int | None,
    market_ticker: str | None,
) -> dict[str, Any] | None:
    """Health-event fields when the book can no longer be trusted."""
    if result.ok:
        return None
    return {
        "health_reason": result.invalid_reason or "orderbook_invalid",
        "book_valid": False,
        "seq": seq,
        "market_ticker": market_ticker,
    }


def _levels(rows: Any) -> dict[str, Decimal] | None:
    """Parse `[price, qty]` levels. Negative size is refused, not clamped."""
    if rows is None:
        return {}
    if not isinstance(rows, list):
        return None
    levels: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return None
        price = str(row[0])
        qty = Decimal(str(row[1]))
        if qty < 0:
            return None
        levels[price] = levels.get(price, Decimal("0")) + qty
    return levels


class Orderbook:
    """YES/NO price levels for one subscription sequence."""

    def __init__(self) -> None:
        self.yes: dict[str, Decimal] = {}
        self.no: dict[str, Decimal] = {}
        self.valid = False
        self.seq: int | None = None
        self.sid: int | None = None
        self.market_ticker: str | None = None

    def reset(self) -> None:
        """Drop state after reconnect or ticker rollover. The next snapshot rebuilds it."""
        self.yes = {}
        self.no = {}
        self.valid = False
        self.seq = None
        self.sid = None
        self.market_ticker = None

    def apply_snapshot(self, envelope: dict[str, Any]) -> ApplyResult:
        msg = envelope.get("msg")
        if not isinstance(msg, dict) or "seq" not in envelope or "sid" not in envelope:
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_snapshot")
        yes = _levels(msg.get("yes_dollars_fp"))
        no = _levels(msg.get("no_dollars_fp"))
        ticker = msg.get("market_ticker")
        if yes is None or no is None or not ticker:
            self.valid = False
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_snapshot")
        self.yes = yes
        self.no = no
        self.market_ticker = str(ticker)
        self.sid = int(envelope["sid"])
        self.seq = int(envelope["seq"])
        self.valid = True
        return ApplyResult(ok=True, applied=True)

    def apply_delta(self, envelope: dict[str, Any]) -> ApplyResult:
        msg = envelope.get("msg")
        if not isinstance(msg, dict) or "seq" not in envelope:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_delta")
        seq = int(envelope["seq"])
        if not self.valid or self.seq is None:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="missing_snapshot")
        if seq != self.seq + 1:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="sequence_gap")
        side = msg.get("side")
        if side not in ("yes", "no"):
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_side")
        book = self.yes if side == "yes" else self.no
        price = str(msg.get("price_dollars"))
        try:
            delta = Decimal(str(msg.get("delta_fp")))
        except Exception:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="bad_delta")
        new_qty = book.get(price, Decimal("0")) + delta
        if new_qty < 0:
            self._invalidate()
            return ApplyResult(ok=False, need_snapshot=True, invalid_reason="negative_level")
        if new_qty == 0:
            book.pop(price, None)
        else:
            book[price] = new_qty
        self.seq = seq
        if "sid" in envelope:
            self.sid = int(envelope["sid"])
        return ApplyResult(ok=True, applied=True)

    def _invalidate(self) -> None:
        self.valid = False
