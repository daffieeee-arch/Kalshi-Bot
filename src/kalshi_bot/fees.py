"""Kalshi quadratic taker fee, rounded the way the docs describe."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

# General (quadratic) taker model used on KXBTC15M: 0.07 * C * P * (1-P).
# Trade fee is then rounded up to $0.000001 (docs.kalshi.com/getting_started/fee_rounding).
_TAKER_RATE = Decimal("0.07")
_SIX_DP = Decimal("0.000001")


def ceil_6dp(amount: Decimal) -> Decimal:
    """Round up to the nearest $0.000001."""
    return amount.quantize(_SIX_DP, rounding=ROUND_CEILING)


def quadratic_taker_fee(
    contracts: Decimal,
    price: Decimal,
    *,
    multiplier: Decimal = Decimal("1"),
) -> Decimal:
    """Taker fee in dollars for a quadratic series."""
    if contracts <= 0:
        return Decimal("0")
    model = _TAKER_RATE * contracts * price * (Decimal("1") - price) * multiplier
    return ceil_6dp(model)
