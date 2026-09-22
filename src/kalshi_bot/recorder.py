"""24/7 KXBTC15M recorder. Read-only demo market data; it never places orders."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_bot.brti import parse_brti, parse_brti_5hz
from kalshi_bot.client import KalshiDemoClient
from kalshi_bot.clockcheck import clock_status, disk_status
from kalshi_bot.config import DEMO_REST_BASE, DEMO_WS_URL, SERIES_TICKER_BTC_15M, Settings, load_settings
from kalshi_bot.discover import is_currently_open
from kalshi_bot.orderbook import Orderbook, health_from_apply
from kalshi_bot.record import SegmentWriter
from kalshi_bot.ws import (
    CommandIds,
    backoff_delay,
    connect,
    parse_trade,
    snapshot_request,
    subscribe_message,
    update_markets_message,
    ws_auth_headers,
)

_SECRET_PARTS = ("signature", "private", "api_key", "pem", "secret", "password", "token")
_MARKET_FIELDS = (
    "ticker",
    "event_ticker",
    "status",
    "title",
    "subtitle",
    "yes_sub_title",
    "floor_strike",
    "cap_strike",
    "open_time",
    "close_time",
    "expiration_time",
    "expected_expiration_time",
    "result",
    "settlement_value_dollars",
    "tick_size",
)
_SETTLED = frozenset({"settled", "finalized", "determined"})


class RecorderLockError(RuntimeError):
    """Raised when another recorder already holds the data-directory lock."""


class FeedStale(Exception):
    """A feed that had been live went silent long enough to force a reconnect."""

    def __init__(self, feed: str) -> None:
        super().__init__(feed)
        self.feed = feed


class ProcessLock:
    """Exclusive non-blocking flock. A second process fails immediately."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RecorderLockError(f"recorder already running (lock {self.path})") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


@dataclass
class LiveSubs:
    orderbook_sid: int | None = None
    trade_sid: int | None = None
    orderbook_ticker: str | None = None
    trade_ticker: str | None = None


def static_subscriptions(ids: CommandIds) -> list[dict[str, Any]]:
    """Lifecycle and BRTI stay up across ticker changes."""
    return [
        subscribe_message(ids.next(), ["market_lifecycle_v2"]),
        subscribe_message(ids.next(), ["cfbenchmarks_value"], index_ids=["BRTI"]),
        subscribe_message(ids.next(), ["cfbenchmarks_value_5hz"], index_ids=["BRTI"]),
    ]


def rollover_commands(
    state: LiveSubs,
    new_ticker: str | None,
    ids: CommandIds,
) -> list[tuple[str, dict[str, Any]]]:
    """Switch orderbook and trade subscriptions. BRTI commands are never included."""
    if new_ticker == state.orderbook_ticker and new_ticker == state.trade_ticker:
        return []
    commands: list[tuple[str, dict[str, Any]]] = []
    if state.orderbook_sid is not None and state.orderbook_ticker:
        commands.append(
            (
                "orderbook",
                update_markets_message(
                    ids.next(), state.orderbook_sid, "delete_markets", [state.orderbook_ticker]
                ),
            )
        )
    if state.trade_sid is not None and state.trade_ticker:
        commands.append(
            (
                "trade",
                update_markets_message(ids.next(), state.trade_sid, "delete_markets", [state.trade_ticker]),
            )
        )
    if new_ticker:
        if state.orderbook_sid is not None:
            commands.append(
                (
                    "orderbook",
                    update_markets_message(ids.next(), state.orderbook_sid, "add_markets", [new_ticker]),
                )
            )
        else:
            commands.append(
                ("orderbook", subscribe_message(ids.next(), ["orderbook_delta"], market_ticker=new_ticker))
            )
        if state.trade_sid is not None:
            commands.append(
                ("trade", update_markets_message(ids.next(), state.trade_sid, "add_markets", [new_ticker]))
            )
        else:
            commands.append(("trade", subscribe_message(ids.next(), ["trade"], market_ticker=new_ticker)))
    state.orderbook_ticker = new_ticker
    state.trade_ticker = new_ticker
    return commands


def select_active_market(markets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Earliest still-open KXBTC15M window. Empty input is not an error."""
    open_markets = [market for market in markets if market.get("ticker") and is_currently_open(market)]
    if not open_markets:
        return None
    open_markets.sort(key=lambda market: market.get("close_time") or "")
    return open_markets[0]


def log_event(event: str, **fields: Any) -> None:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        lowered = key.lower()
        if any(part in lowered for part in _SECRET_PARTS):
            continue
        if isinstance(value, str) and ("BEGIN " in value or "KALSHI-ACCESS" in value):
            continue
        safe[key] = value
    print(
        json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **safe},
            default=str,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _series_fee(payload: dict[str, Any]) -> dict[str, Any]:
    series = payload.get("series") if isinstance(payload.get("series"), dict) else {}
    return {"fee_type": series.get("fee_type"), "fee_multiplier": series.get("fee_multiplier")}


def _event_fee(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    return {
        "fee_type_override": event.get("fee_type_override"),
        "fee_multiplier_override": event.get("fee_multiplier_override"),
    }


def _market_normalized(market: dict[str, Any], series_fee: dict[str, Any], event_fee: dict[str, Any]) -> dict[str, Any]:
    normalized = {key: market.get(key) for key in _MARKET_FIELDS if key in market}
    normalized.update(series_fee)
    for key, value in event_fee.items():
        if value is not None:
            normalized[key] = value
    normalized["event_kind"] = "metadata"
    normalized["market_ticker"] = market.get("ticker")
    return normalized


@dataclass
class _Session:
    writer: SegmentWriter
    book: Orderbook = field(default_factory=Orderbook)
    subs: LiveSubs = field(default_factory=LiveSubs)
    ids: CommandIds = field(default_factory=CommandIds)
    pending: dict[int, str] = field(default_factory=dict)
    active_ticker: str | None = None
    brti_unavailable: bool = False
    brti5_unavailable: bool = False
    snapshot_requested: bool = False
    messages: int = 0
    started: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    last_brti: float | None = None
    last_book: float | None = None
    book_armed_at: float | None = None
    brti_silent_noted: bool = False
    book_silent_noted: bool = False
    seen_markets: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    settlement_done: set[str] = field(default_factory=set)
    commands: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, purpose: str, command: dict[str, Any]) -> None:
        self.pending[int(command["id"])] = purpose
        self.commands.append(command)


def _sid_of(envelope: dict[str, Any]) -> int | None:
    msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
    raw = msg.get("sid", envelope.get("sid"))
    return None if raw is None else int(raw)


def _remember_sid(session: _Session, envelope: dict[str, Any]) -> None:
    purpose = session.pending.pop(envelope.get("id"), None)
    msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
    channel = msg.get("channel") or purpose
    sid = _sid_of(envelope)
    if sid is None:
        return
    if channel == "orderbook_delta":
        session.subs.orderbook_sid = sid
        session.book.sid = sid
    elif channel == "trade":
        session.subs.trade_sid = sid


def _note_channel_error(session: _Session, envelope: dict[str, Any], received_at: datetime) -> None:
    purpose = session.pending.pop(envelope.get("id"), None)
    msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
    code = msg.get("code")
    if purpose == "brti":
        session.brti_unavailable = True
        reason = "brti_unavailable"
    elif purpose == "brti5":
        session.brti5_unavailable = True
        reason = "brti_5hz_unavailable"
    else:
        reason = "ws_error"
    session.writer.write(
        "health",
        received_at=received_at,
        payload=envelope,
        normalized={"health_reason": reason, "event_kind": str(code) if code is not None else None},
        now=received_at,
    )
    log_event("ws_error", purpose=purpose, code=code, reason=reason)


def handle_message(session: _Session, envelope: dict[str, Any], received_at: datetime) -> None:
    """Apply one socket message. Subscription repairs are queued on `session.commands`."""
    session.messages += 1
    kind = envelope.get("type")
    if kind == "subscribed":
        _remember_sid(session, envelope)
        log_event("subscribed", sid=_sid_of(envelope), channel=(envelope.get("msg") or {}).get("channel"))
        return
    if kind == "error":
        _note_channel_error(session, envelope, received_at)
        return
    if kind == "orderbook_snapshot":
        result = session.book.apply_snapshot(envelope)
        session.snapshot_requested = False
        session.last_book = asyncio.get_running_loop().time()
        session.book_silent_noted = False
        msg = envelope.get("msg") or {}
        session.writer.write(
            "orderbook",
            received_at=received_at,
            payload=envelope,
            source_ts=msg.get("ts_ms"),
            normalized={
                "market_ticker": msg.get("market_ticker"),
                "seq": envelope.get("seq"),
                "sid": envelope.get("sid"),
                "event_kind": "snapshot",
                "book_valid": session.book.valid,
            },
            now=received_at,
        )
        if not result.ok:
            _write_book_health(session, envelope, result, received_at)
        return
    if kind == "orderbook_delta":
        before = session.book.market_ticker
        result = session.book.apply_delta(envelope)
        if result.applied:
            session.last_book = asyncio.get_running_loop().time()
            session.book_silent_noted = False
        msg = envelope.get("msg") or {}
        session.writer.write(
            "orderbook",
            received_at=received_at,
            payload=envelope,
            source_ts=msg.get("ts_ms"),
            normalized={
                "market_ticker": msg.get("market_ticker") or before,
                "seq": envelope.get("seq"),
                "sid": envelope.get("sid"),
                "event_kind": "delta",
                "side": msg.get("side"),
                "price_dollars": msg.get("price_dollars"),
                "delta_fp": msg.get("delta_fp"),
                "book_valid": session.book.valid,
            },
            now=received_at,
        )
        if not result.ok:
            _write_book_health(session, envelope, result, received_at)
            _request_snapshot(session, msg.get("market_ticker") or session.active_ticker)
        return
    if kind == "trade":
        parsed = parse_trade(envelope)
        session.writer.write(
            "trade",
            received_at=received_at,
            payload=envelope,
            source_ts=parsed.get("source_ts"),
            normalized=parsed,
            now=received_at,
        )
        return
    if kind == "cfbenchmarks_value":
        parsed = parse_brti(envelope)
        session.last_brti = asyncio.get_running_loop().time()
        session.brti_silent_noted = False
        session.brti_unavailable = False
        session.writer.write(
            "brti",
            received_at=received_at,
            payload=envelope,
            source_ts=parsed.get("source_ts"),
            provider_received_at=parsed.get("provider_received_at"),
            normalized=parsed,
            now=received_at,
        )
        return
    if kind == "cfbenchmarks_value_5hz":
        parsed = parse_brti_5hz(envelope)
        session.writer.write(
            "brti_5hz",
            received_at=received_at,
            payload=envelope,
            source_ts=parsed.get("source_ts"),
            provider_received_at=parsed.get("provider_received_at"),
            normalized=parsed,
            now=received_at,
        )
        return
    if kind == "market_lifecycle_v2":
        msg = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else {}
        session.writer.write(
            "lifecycle",
            received_at=received_at,
            payload=envelope,
            normalized={
                "market_ticker": msg.get("market_ticker"),
                "event_ticker": msg.get("event_ticker"),
                "event_kind": msg.get("event_type"),
                "result": msg.get("result"),
                "status": msg.get("status"),
            },
            now=received_at,
        )
        return
    session.writer.write(
        "health",
        received_at=received_at,
        payload=envelope,
        normalized={"health_reason": "unhandled_message", "event_kind": kind},
        now=received_at,
    )


def _write_book_health(session: _Session, envelope: dict[str, Any], result: Any, received_at: datetime) -> None:
    health = health_from_apply(
        result,
        seq=envelope.get("seq"),
        market_ticker=(envelope.get("msg") or {}).get("market_ticker"),
    )
    if health is None:
        return
    session.writer.write("health", received_at=received_at, payload=envelope, normalized=health, now=received_at)
    log_event("orderbook_invalid", reason=health["health_reason"], ticker=health.get("market_ticker"))


def _request_snapshot(session: _Session, ticker: str | None) -> None:
    if session.snapshot_requested or session.subs.orderbook_sid is None or not ticker:
        return
    session.snapshot_requested = True
    session.queue("snapshot", snapshot_request(session.ids.next(), session.subs.orderbook_sid, ticker))


def _check_stale(session: _Session, received_at: datetime) -> None:
    now = asyncio.get_running_loop().time()
    if not session.brti_unavailable:
        if session.last_brti is None and now - session.started > 5 and not session.brti_silent_noted:
            session.brti_silent_noted = True
            session.writer.write(
                "health",
                received_at=received_at,
                payload={"type": "health", "feed": "brti"},
                normalized={"health_reason": "brti_silent"},
                now=received_at,
            )
            log_event("brti_silent")
        elif session.last_brti is not None and now - session.last_brti > 5 and not session.brti_silent_noted:
            session.brti_silent_noted = True
            session.writer.write(
                "health",
                received_at=received_at,
                payload={"type": "health", "feed": "brti"},
                normalized={"health_reason": "brti_stale"},
                now=received_at,
            )
            log_event("brti_stale")
        if session.last_brti is not None and now - session.last_brti > 30:
            raise FeedStale("brti")
    if session.active_ticker is None or session.book_armed_at is None:
        return
    anchor = session.book_armed_at if session.last_book is None else session.last_book
    quiet_for = now - anchor
    if quiet_for > 15 and not session.book_silent_noted:
        session.book_silent_noted = True
        reason = "missing_snapshot" if session.last_book is None else "book_stale"
        session.writer.write(
            "health",
            received_at=received_at,
            payload={"type": "health", "feed": "orderbook"},
            normalized={"health_reason": reason, "market_ticker": session.active_ticker, "book_valid": False},
            now=received_at,
        )
        log_event(reason, ticker=session.active_ticker)
        _request_snapshot(session, session.active_ticker)
    if quiet_for > 45:
        raise FeedStale("orderbook")


def _record_market(
    session: _Session,
    market: dict[str, Any],
    series_fee: dict[str, Any],
    event_fee: dict[str, Any],
    *,
    event_kind: str,
) -> None:
    received_at = _utc()
    normalized = _market_normalized(market, series_fee, event_fee)
    normalized["event_kind"] = event_kind
    session.writer.write(
        "market",
        received_at=received_at,
        payload=market,
        normalized=normalized,
        now=received_at,
    )


def _fingerprint(market: dict[str, Any]) -> tuple[Any, ...]:
    return (
        market.get("ticker"),
        market.get("status"),
        market.get("result"),
        market.get("close_time"),
        market.get("floor_strike"),
    )


async def _send_queued(ws: Any, session: _Session) -> None:
    while session.commands:
        command = session.commands.pop(0)
        await ws.send(json.dumps(command))


async def _run_session(
    settings: Settings,
    client: KalshiDemoClient,
    writer: SegmentWriter,
    stop: asyncio.Event,
) -> None:
    headers = ws_auth_headers(settings)
    session = _Session(writer=writer)
    async with connect(settings, headers) as ws:
        static = static_subscriptions(session.ids)
        for purpose, command in zip(("lifecycle", "brti", "brti5"), static, strict=True):
            session.queue(purpose, command)
        await _send_queued(ws, session)
        log_event("session_start", ws="demo")
        series_fee = _series_fee(await asyncio.to_thread(client.get_series, SERIES_TICKER_BTC_15M))
        event_fees: dict[str, dict[str, Any]] = {}
        next_discover = 0.0
        next_settlement = 0.0
        next_stats = asyncio.get_running_loop().time() + 10
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
            except TimeoutError:
                raw = None
            received_at = _utc()
            if raw is not None:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    writer.write(
                        "health",
                        received_at=received_at,
                        payload={"type": "health"},
                        normalized={"health_reason": "bad_json"},
                        now=received_at,
                    )
                    envelope = None
                if isinstance(envelope, dict):
                    handle_message(session, envelope, received_at)
                    await _send_queued(ws, session)
            loop_time = asyncio.get_running_loop().time()
            if loop_time >= next_discover:
                next_discover = loop_time + 5
                await _discover(client, session, series_fee, event_fees)
                await _send_queued(ws, session)
            if loop_time >= next_settlement:
                next_settlement = loop_time + 30
                await _poll_settlements(client, session, series_fee, event_fees)
            if loop_time >= next_stats:
                next_stats = loop_time + 10
                log_event(
                    "stats",
                    ticker=session.active_ticker,
                    messages=session.messages,
                    files_completed=writer.files_completed,
                    book_valid=session.book.valid,
                    brti_unavailable=session.brti_unavailable,
                    brti5_unavailable=session.brti5_unavailable,
                )
            _check_stale(session, received_at)
            await _send_queued(ws, session)


async def _discover(
    client: KalshiDemoClient,
    session: _Session,
    series_fee: dict[str, Any],
    event_fees: dict[str, dict[str, Any]],
) -> None:
    try:
        payload = await asyncio.to_thread(
            client.get_markets,
            series_ticker=SERIES_TICKER_BTC_15M,
            status="open",
            limit=100,
        )
    except Exception as exc:
        received_at = _utc()
        session.writer.write(
            "health",
            received_at=received_at,
            payload={"type": "health"},
            normalized={"health_reason": "discovery_error", "event_kind": type(exc).__name__},
            now=received_at,
        )
        log_event("discovery_error", error=type(exc).__name__)
        return
    markets = list(payload.get("markets") or [])
    active = select_active_market(markets)
    ticker = None if active is None else str(active["ticker"])
    if ticker != session.active_ticker:
        if ticker is None:
            log_event("no_open_market")
        else:
            log_event("active_market", ticker=ticker, close_time=active.get("close_time") if active else None)
        session.book.reset()
        session.last_book = None
        session.book_armed_at = asyncio.get_running_loop().time()
        session.book_silent_noted = False
        session.snapshot_requested = False
        for purpose, command in rollover_commands(session.subs, ticker, session.ids):
            session.queue(purpose, command)
        session.active_ticker = ticker
    if active is not None:
        fingerprint = _fingerprint(active)
        if session.seen_markets.get(str(active["ticker"])) != fingerprint:
            event_ticker = str(active.get("event_ticker") or "")
            event_fee = event_fees.get(event_ticker, {})
            if event_ticker and event_ticker not in event_fees:
                try:
                    event_fee = _event_fee(await asyncio.to_thread(client.get_event, event_ticker))
                except Exception:
                    event_fee = {}
                event_fees[event_ticker] = event_fee
            _record_market(session, active, series_fee, event_fee, event_kind="metadata")
            session.seen_markets[str(active["ticker"])] = fingerprint


async def _poll_settlements(
    client: KalshiDemoClient,
    session: _Session,
    series_fee: dict[str, Any],
    event_fees: dict[str, dict[str, Any]],
) -> None:
    now = _utc()
    for ticker, fingerprint in list(session.seen_markets.items()):
        if ticker in session.settlement_done:
            continue
        close_time = fingerprint[3]
        if not isinstance(close_time, str):
            continue
        try:
            close_at = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        except ValueError:
            continue
        if close_at > now:
            continue
        try:
            payload = await asyncio.to_thread(client.get_market, ticker)
        except Exception as exc:
            log_event("settlement_poll_error", ticker=ticker, error=type(exc).__name__)
            continue
        market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
        status = str(market.get("status") or "").lower()
        if market.get("result") or status in _SETTLED:
            event_ticker = str(market.get("event_ticker") or "")
            _record_market(
                session,
                market,
                series_fee,
                event_fees.get(event_ticker, {}),
                event_kind="settlement",
            )
            session.settlement_done.add(ticker)
            log_event("settlement_recorded", ticker=ticker, status=status, result=market.get("result"))


async def run_recorder(settings: Settings, data_root: Path, stop: asyncio.Event) -> None:
    if settings.rest_base != DEMO_REST_BASE or settings.ws_url != DEMO_WS_URL or settings.env != "demo":
        raise ValueError("recorder refuses non-demo settings")
    data_root.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(data_root / "recorder.lock")
    lock.acquire()
    writer = SegmentWriter(data_root / "raw", source_env="demo")
    log_event("startup", clock=clock_status(), disk=disk_status(data_root), source_env="demo")
    attempt = 0
    try:
        with KalshiDemoClient(settings) as client:
            while not stop.is_set():
                try:
                    await _run_session(settings, client, writer, stop)
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except FeedStale as exc:
                    attempt += 1
                    await _reconnect_wait(writer, stop, attempt, exc.feed)
                except Exception as exc:
                    attempt += 1
                    log_event("session_error", error=type(exc).__name__)
                    await _reconnect_wait(writer, stop, attempt, type(exc).__name__)
    finally:
        writer.close()
        lock.release()
        log_event("shutdown")


async def _reconnect_wait(writer: SegmentWriter, stop: asyncio.Event, attempt: int, reason: str) -> None:
    delay = backoff_delay(attempt)
    received_at = _utc()
    writer.write(
        "health",
        received_at=received_at,
        payload={"type": "health", "reason": reason},
        normalized={"health_reason": "reconnect", "event_kind": reason},
        now=received_at,
    )
    log_event("reconnect", reason=reason, attempt=attempt, delay_seconds=round(delay, 3))
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        return


async def serve(settings: Settings, data_root: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await run_recorder(settings, data_root, stop)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="record-kxbtc15m",
        description="Record Kalshi DEMO KXBTC15M market data. Does not place orders.",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("KALSHI_DATA_DIR", "data"),
        help="Directory for the lock and raw segments (default: ./data)",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(load_settings(), Path(args.data_dir)))
    except RecorderLockError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
