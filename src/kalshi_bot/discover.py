"""Discover current open KXBTC15M (Bitcoin 15-minute up/down) markets on DEMO."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from kalshi_bot.client import KalshiDemoClient
from kalshi_bot.config import SERIES_TICKER_BTC_15M

_OPENISH = frozenset({"open", "active"})
_NY = ZoneInfo("America/New_York")
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_TICKER_PREFIX = "KXBTC15M-"


def parse_kxbtc15m_close(ticker: str) -> datetime | None:
    """Parse KXBTC15M-26SEP221015-15 as 2026-09-22 10:15 America/New_York."""
    if not ticker.startswith(_TICKER_PREFIX):
        return None
    body = ticker[len(_TICKER_PREFIX) :]
    stamp, sep, _suffix = body.partition("-")
    if not sep or len(stamp) != 11:
        return None
    year = 2000 + int(stamp[0:2])
    month = _MONTHS.get(stamp[2:5])
    if month is None:
        return None
    day = int(stamp[5:7])
    hour = int(stamp[7:9])
    minute = int(stamp[9:11])
    return datetime(year, month, day, hour, minute, tzinfo=_NY)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_currently_open(market: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True if market looks tradable: open/active status and close_time in the future."""
    now = now or datetime.now(timezone.utc)
    status = (market.get("status") or "").lower()
    if status not in _OPENISH:
        return False
    close_time = _parse_ts(market.get("close_time"))
    if close_time is not None and close_time <= now:
        return False
    return True


def summarize_market(market: dict[str, Any]) -> dict[str, Any]:
    """Pick the fields we care about for CLI / smoke output."""
    return {
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title"),
        "status": market.get("status"),
        "close_time": market.get("close_time"),
        "floor_strike": market.get("floor_strike"),
        "yes_bid": market.get("yes_bid_dollars"),
        "yes_ask": market.get("yes_ask_dollars"),
        "no_bid": market.get("no_bid_dollars"),
        "no_ask": market.get("no_ask_dollars"),
        "last_price": market.get("last_price_dollars"),
        "yes_sub_title": market.get("yes_sub_title"),
    }


def discover_btc_15m(client: KalshiDemoClient) -> list[dict[str, Any]]:
    """Return currently open KXBTC15M markets (summary dicts), newest close first."""
    payload = client.get_markets(series_ticker=SERIES_TICKER_BTC_15M, status="open", limit=100)
    markets = payload.get("markets") or []
    open_markets = [m for m in markets if is_currently_open(m)]
    open_markets.sort(key=lambda m: m.get("close_time") or "", reverse=True)
    return [summarize_market(m) for m in open_markets]


def format_market_line(summary: dict[str, Any]) -> str:
    return (
        f"{summary['ticker']}\n"
        f"  title:        {summary['title']}\n"
        f"  event:        {summary['event_ticker']}\n"
        f"  status:       {summary['status']}\n"
        f"  close_time:   {summary['close_time']}\n"
        f"  floor_strike: {summary['floor_strike']}\n"
        f"  quotes:       yes {summary['yes_bid']}/{summary['yes_ask']}  "
        f"no {summary['no_bid']}/{summary['no_ask']}  "
        f"last {summary['last_price']}\n"
        f"  yes_sub:      {summary['yes_sub_title']}"
    )
