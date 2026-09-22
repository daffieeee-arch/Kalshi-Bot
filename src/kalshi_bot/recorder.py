"""24/7 production recorder: WebSocket events, no REST book polling."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import random
import signal
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from kalshi_bot.client import KalshiReadClient
from kalshi_bot.config import BRTI_INDEX_ID, SERIES_TICKER_BTC_15M, Settings, require_prod_credentials
from kalshi_bot.discover import is_currently_open, summarize_market
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.paper import PaperIntent, PaperLedger
from kalshi_bot.record import JsonlWriter, extract_ts_ms
from kalshi_bot.ws import ProductionWebSocket

log = logging.getLogger("kalshi_bot.recorder")

DEFAULT_JSONL = Path("data/prod-kxbtc15m.jsonl")
DEFAULT_LOCK = Path("data/recorder.lock")
_BACKOFF_CAP_S = 60.0


@dataclass
class Subscriptions:
    orderbook: int | None = None
    trade: int | None = None
    brti: int | None = None
    brti_5hz: int | None = None


class Recorder:
    """Stay on one production socket; roll markets with update_subscription."""

    def __init__(
        self,
        settings: Settings,
        *,
        jsonl_path: Path = DEFAULT_JSONL,
        lock_path: Path = DEFAULT_LOCK,
        paper: PaperLedger | None = None,
        seconds: float | None = None,
    ) -> None:
        self.settings = require_prod_credentials(settings)
        self.jsonl_path = jsonl_path
        self.lock_path = lock_path
        self.paper = paper or PaperLedger()
        self.seconds = seconds
        self.book = OrderbookState()
        self.subs = Subscriptions()
        self.ticker: str | None = None
        self.floor_strike: Decimal | None = None
        self._stop = asyncio.Event()
        self._lock_fh: TextIO | None = None
        self._reconnects = 0
        self._last_discover = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> int:
        self._acquire_lock()
        writer = JsonlWriter(self.jsonl_path)
        rest = KalshiReadClient(self.settings)
        started = asyncio.get_running_loop().time()
        try:
            while not self._stop.is_set():
                if self._expired(started):
                    break
                try:
                    await self._session(rest, writer, started)
                    self._reconnects = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("production websocket session ended")
                    await self._backoff()
            return 0
        finally:
            writer.close()
            rest.close()
            self._release_lock()

    def _expired(self, started: float) -> bool:
        if self.seconds is None:
            return False
        return asyncio.get_running_loop().time() - started >= self.seconds

    async def _session(
        self,
        rest: KalshiReadClient,
        writer: JsonlWriter,
        started: float,
    ) -> None:
        market = _open_market(rest)
        ws = ProductionWebSocket(self.settings)
        await ws.connect()
        try:
            await self._subscribe(ws, market)
            async for message in ws.messages():
                if self._stop.is_set() or self._expired(started):
                    break
                await self._handle(message, ws, writer)
                await self._maybe_discover(ws, rest)
        finally:
            self.book.reset()
            self.subs = Subscriptions()
            await ws.close()

    async def _maybe_discover(self, ws: ProductionWebSocket, rest: KalshiReadClient) -> None:
        now = asyncio.get_running_loop().time()
        if self.ticker is not None and now - self._last_discover < 15.0:
            return
        self._last_discover = now
        nxt = _open_market(rest)
        if nxt and nxt.get("ticker") != self.ticker:
            await self._roll(ws, str(nxt["ticker"]), nxt.get("floor_strike"))

    async def _subscribe(self, ws: ProductionWebSocket, market: dict[str, Any] | None) -> None:
        self.ticker = None
        self.floor_strike = None
        if market:
            self.ticker = str(market["ticker"])
            strike = market.get("floor_strike")
            self.floor_strike = Decimal(str(strike)) if strike is not None else None
            for intent in self.paper.intents:
                if not intent.market_ticker:
                    intent.market_ticker = self.ticker
        await ws.subscribe(channels=["cfbenchmarks_value"], index_ids=[BRTI_INDEX_ID])
        await ws.subscribe(channels=["cfbenchmarks_value_5hz"], index_ids=[BRTI_INDEX_ID])
        if self.ticker:
            await ws.subscribe(channels=["orderbook_delta"], market_tickers=[self.ticker])
            await ws.subscribe(channels=["trade"], market_tickers=[self.ticker])
        else:
            log.warning("no open KXBTC15M window; waiting on BRTI only")

    async def _handle(
        self,
        message: dict[str, Any],
        ws: ProductionWebSocket,
        writer: JsonlWriter,
    ) -> None:
        msg_type = str(message.get("type") or "unknown")
        writer.write(message, stream=msg_type, ts_ms=extract_ts_ms(message))
        payload = message.get("msg") or {}

        if msg_type == "subscribed":
            self._remember_sid(str(payload.get("channel")), payload.get("sid"))
            return
        if msg_type == "error":
            log.error("ws error %s", payload)
            return
        if msg_type == "orderbook_snapshot":
            result = self.book.apply_snapshot(message)
            if result.ok:
                self.paper.on_book(self.book, ts_ms=extract_ts_ms(message))
            return
        if msg_type == "orderbook_delta":
            result = self.book.apply_delta(message)
            if result.need_snapshot and self.subs.orderbook is not None and self.ticker:
                await ws.update_subscription(
                    sid=self.subs.orderbook,
                    action="get_snapshot",
                    market_tickers=[self.ticker],
                )
            elif result.ok:
                self.paper.on_book(self.book, ts_ms=extract_ts_ms(message))
            return
        if msg_type == "trade":
            self.paper.on_trade(message)
            return
        if msg_type == "cfbenchmarks_value":
            self._maybe_settle(payload)
            return
        if msg_type in {
            "ok",
            "cfbenchmarks_value_5hz",
            "cfbenchmarks_value_indexlist",
            "cfbenchmarks_value_5hz_indexlist",
            "unsubscribed",
            "market_lifecycle_v2",
        }:
            return

    def _remember_sid(self, channel: str, sid: Any) -> None:
        if sid is None:
            return
        value = int(sid)
        if channel == "orderbook_delta":
            self.subs.orderbook = value
        elif channel == "trade":
            self.subs.trade = value
        elif channel == "cfbenchmarks_value":
            self.subs.brti = value
        elif channel == "cfbenchmarks_value_5hz":
            self.subs.brti_5hz = value

    async def _roll(self, ws: ProductionWebSocket, ticker: str, floor_strike: Any) -> None:
        old = self.ticker
        if self.subs.orderbook is not None and old:
            await ws.update_subscription(
                sid=self.subs.orderbook,
                action="delete_markets",
                market_tickers=[old],
            )
            await ws.update_subscription(
                sid=self.subs.orderbook,
                action="add_markets",
                market_tickers=[ticker],
            )
        if self.subs.trade is not None and old:
            await ws.update_subscription(
                sid=self.subs.trade,
                action="delete_markets",
                market_tickers=[old],
            )
            await ws.update_subscription(
                sid=self.subs.trade,
                action="add_markets",
                market_tickers=[ticker],
            )
        if self.subs.orderbook is None:
            await ws.subscribe(channels=["orderbook_delta"], market_tickers=[ticker])
        if self.subs.trade is None:
            await ws.subscribe(channels=["trade"], market_tickers=[ticker])
        self.book.reset()
        self.ticker = ticker
        self.floor_strike = Decimal(str(floor_strike)) if floor_strike is not None else None
        log.info("rolled to %s", ticker)

    def _maybe_settle(self, payload: dict[str, Any]) -> None:
        window = payload.get("last_60s_windowed_average_15min")
        if not isinstance(window, dict) or self.ticker is None or self.floor_strike is None:
            return
        if int(window.get("window_size") or 0) < 60:
            return
        close_avg = Decimal(str(window["value"]))
        self.paper.on_settlement(
            market_ticker=self.ticker,
            close_avg=close_avg,
            floor_strike=self.floor_strike,
        )

    async def _backoff(self) -> None:
        self._reconnects += 1
        delay = min(_BACKOFF_CAP_S, (2 ** min(self._reconnects, 5)) + random.random())
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = self.lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_fh.close()
            self._lock_fh = None
            raise RuntimeError(f"recorder lock is held: {self.lock_path}") from exc
        self._lock_fh.write(f"pid={os.getpid()}\n")
        self._lock_fh.flush()

    def _release_lock(self) -> None:
        if self._lock_fh is None:
            return
        fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        self._lock_fh.close()
        self._lock_fh = None


def _open_market(rest: KalshiReadClient) -> dict[str, Any] | None:
    raw = rest.get_markets(series_ticker=SERIES_TICKER_BTC_15M, status="open", limit=50)
    open_markets = [m for m in (raw.get("markets") or []) if is_currently_open(m)]
    open_markets.sort(key=lambda m: m.get("close_time") or "")
    if not open_markets:
        return None
    return summarize_market(open_markets[0])


async def run_recorder(
    settings: Settings,
    *,
    seconds: float | None = None,
    paper: PaperIntent | None = None,
) -> int:
    ledger = PaperLedger()
    if paper is not None:
        ledger.add(paper)
    recorder = Recorder(settings, seconds=seconds, paper=ledger)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, recorder.request_stop)
    return await recorder.run()
