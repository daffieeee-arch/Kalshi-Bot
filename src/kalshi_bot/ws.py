"""Demo WebSocket handshake, subscription commands, and trade parsing.

The socket is market-data only. Nothing here submits an order.
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Callable
from typing import Any

import websockets

from kalshi_bot.auth import auth_headers, load_private_key
from kalshi_bot.config import DEMO_WS_URL, Settings

WS_SIGN_PATH = "/trade-api/ws/v2"
_AGGRESSOR_FIELDS = ("taker_side", "taker_outcome_side", "taker_book_side")


class CommandIds:
    """Monotonic command ids for one WebSocket session. Zero is reserved by Kalshi."""

    def __init__(self) -> None:
        self._next = 1

    def next(self) -> int:
        value = self._next
        self._next += 1
        return value


def ws_auth_headers(settings: Settings, *, timestamp_ms: int | None = None) -> dict[str, str]:
    """Sign `timestamp + GET + /trade-api/ws/v2` for the demo socket only."""
    if settings.ws_url != DEMO_WS_URL:
        raise ValueError(
            f"Refusing non-demo WebSocket URL: {settings.ws_url!r}. Expected {DEMO_WS_URL!r}."
        )
    if not settings.has_credentials or settings.api_key_id is None or settings.private_key_path is None:
        raise RuntimeError(
            "WebSocket auth requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH."
        )
    private_key = load_private_key(settings.private_key_path)
    return auth_headers(
        api_key_id=settings.api_key_id,
        private_key=private_key,
        method="GET",
        url_or_path=WS_SIGN_PATH,
        timestamp_ms=timestamp_ms,
    )


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff in seconds with jitter, capped at 60s. `attempt` starts at 1."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    base = min(60.0, float(2 ** (attempt - 1)))
    return base * (0.5 + 0.5 * rng())


def subscribe_message(cmd_id: int, channels: list[str], **params: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"channels": channels}
    body.update(params)
    return {"id": cmd_id, "cmd": "subscribe", "params": body}


def update_markets_message(
    cmd_id: int,
    sid: int,
    action: str,
    market_tickers: list[str],
) -> dict[str, Any]:
    if action not in {"add_markets", "delete_markets", "get_snapshot"}:
        raise ValueError(f"unsupported subscription action: {action}")
    return {
        "id": cmd_id,
        "cmd": "update_subscription",
        "params": {
            "sids": [sid],
            "action": action,
            "market_tickers": list(market_tickers),
        },
    }


def snapshot_request(cmd_id: int, sid: int, market_ticker: str) -> dict[str, Any]:
    return update_markets_message(cmd_id, sid, "get_snapshot", [market_ticker])


def parse_trade(envelope: dict[str, Any]) -> dict[str, Any]:
    """Copy trade fields the API actually sent. Aggressor sides are not inferred."""
    msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
    parsed: dict[str, Any] = {
        "trade_id": msg.get("trade_id"),
        "market_ticker": msg.get("market_ticker"),
        "yes_price_dollars": msg.get("yes_price_dollars"),
        "no_price_dollars": msg.get("no_price_dollars"),
        "count_fp": msg.get("count_fp"),
        "source_ts": msg.get("ts_ms"),
    }
    if "is_block_trade" in msg:
        parsed["is_block_trade"] = msg["is_block_trade"]
    for key in _AGGRESSOR_FIELDS:
        if key in msg:
            parsed[key] = msg[key]
    return parsed


def connect(settings: Settings, headers: dict[str, str]) -> Any:
    """Open the demo WebSocket. Header kwarg follows the installed websockets version."""
    if settings.ws_url != DEMO_WS_URL:
        raise ValueError(f"Refusing non-demo WebSocket URL: {settings.ws_url!r}.")
    parameters = inspect.signature(websockets.connect).parameters
    header_key = "additional_headers" if "additional_headers" in parameters else "extra_headers"
    kwargs: dict[str, Any] = {
        header_key: headers,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 30,
        "max_queue": 1024,
    }
    return websockets.connect(settings.ws_url, **kwargs)
