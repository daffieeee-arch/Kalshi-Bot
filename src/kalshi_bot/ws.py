"""Signed production WebSocket. Handshake matches docs.kalshi.com WS quick start."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from kalshi_bot.auth import auth_headers, load_private_key
from kalshi_bot.config import PROD_WS_URL, WS_SIGN_PATH, Settings


class ProductionWebSocket:
    """Stay-open production socket. Callers reconnect; this object is one session."""

    def __init__(self, settings: Settings) -> None:
        if settings.prod_ws_url != PROD_WS_URL:
            raise ValueError(
                f"Refusing non-production WS url: {settings.prod_ws_url!r}. "
                f"Expected {PROD_WS_URL!r}."
            )
        if not settings.has_prod_credentials:
            raise RuntimeError("Production WebSocket requires a live API key.")
        assert settings.prod_private_key_path is not None
        assert settings.prod_api_key_id is not None
        self.settings = settings
        self._private_key = load_private_key(settings.prod_private_key_path)
        self._ws: ClientConnection | None = None
        self._next_id = 1

    async def connect(self) -> None:
        headers = auth_headers(
            api_key_id=self.settings.prod_api_key_id or "",
            private_key=self._private_key,
            method="GET",
            url_or_path=WS_SIGN_PATH,
        )
        self._ws = await websockets.connect(
            self.settings.prod_ws_url,
            additional_headers=headers,
            open_timeout=20,
        )
        self._next_id = 1

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def send(self, cmd: str, params: dict[str, Any]) -> int:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        msg_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"id": msg_id, "cmd": cmd, "params": params}))
        return msg_id

    async def subscribe(self, *, channels: list[str], **extra: Any) -> int:
        params: dict[str, Any] = {"channels": channels}
        params.update(extra)
        return await self.send("subscribe", params)

    async def update_subscription(self, *, sid: int, action: str, **extra: Any) -> int:
        params: dict[str, Any] = {"sid": sid, "action": action}
        params.update(extra)
        return await self.send("update_subscription", params)

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        async for raw in self._ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            yield json.loads(raw)
