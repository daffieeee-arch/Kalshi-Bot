"""Rebuild live dashboard state by tailing the production JSONL capture."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_bot.client import KalshiReadClient
from kalshi_bot.config import SERIES_TICKER_BTC_15M, Settings
from kalshi_bot.discover import is_currently_open, parse_kxbtc15m_close, summarize_market
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.recorder import DEFAULT_JSONL, DEFAULT_LOCK
from kalshi_bot.session import DEFAULT_SESSION_ROOT, paper_session_dirs, read_session_view
from kalshi_bot.signal import evaluate, sigma_from_samples, signal_to_dict

_DISCOVER_INTERVAL_S = 15.0


def _is_snapshot_line(line: bytes) -> bool:
    return b'"stream":"orderbook_snapshot"' in line or b'"stream": "orderbook_snapshot"' in line
_LOOKBACK_BYTES = None
_CHUNK_BYTES = 4_000_000
_TAPE_MAX = 80
_RATE_WINDOW_S = 5.0
_SPOT_WINDOW = 90
_QUOTE_MAX_AGE_S = 5.0
STREAM_LABELS = {
    "cfbenchmarks_value": "BRTI 1 Hz",
    "cfbenchmarks_value_5hz": "BRTI 5 Hz",
    "orderbook_delta": "book deltas",
    "orderbook_snapshot": "book snapshot",
    "trade": "tape",
    "subscribed": "subscribe ack",
    "ok": "command ack",
    "market_meta": "market roll",
}


def _session_views(dirs: list[Path]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for path in dirs:
        view = read_session_view(path)
        if view is not None:
            views.append(view)
    return views


def _levels(book: dict[Decimal, Decimal], *, reverse: bool) -> list[list[str]]:
    keys = sorted(book, reverse=reverse)
    return [[format(price, "f"), format(book[price], "f")] for price in keys]


def _dec(value: Any) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


class LiveFeed:
    """Follow one JSONL file. Safe to read snapshot() from the HTTP thread."""

    def __init__(
        self,
        jsonl_path: Path = DEFAULT_JSONL,
        lock_path: Path = DEFAULT_LOCK,
        settings: Settings | None = None,
        session_dir: Path = DEFAULT_SESSION_ROOT,
        session_dirs: list[Path] | None = None,
    ) -> None:
        self.jsonl_path = jsonl_path
        self.lock_path = lock_path
        self.settings = settings
        self.session_dir = session_dir
        self.session_dirs = session_dirs if session_dirs is not None else paper_session_dirs(session_dir)
        self.book = OrderbookState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._counts: dict[str, int] = {}
        self._rate_times: deque[tuple[float, str]] = deque()
        self._tape: deque[dict[str, Any]] = deque(maxlen=_TAPE_MAX)
        self._brti: dict[str, Any] = {}
        self._spots: deque[tuple[int | None, Decimal]] = deque(maxlen=_SPOT_WINDOW)
        self._market: dict[str, Any] = {}
        self._last_row_at: str | None = None
        self._book_status = "waiting_snapshot"
        self._error: str | None = None
        self._last_discover = 0.0
        self._book_mono: float | None = None
        self._brti_mono: float | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="dashboard-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            rates = self._rates(now)
            book_ok = self.book.valid
            yes_bid = self.book.best_bid("yes") if book_ok else None
            no_bid = self.book.best_bid("no") if book_ok else None
            yes_ask = self.book.implied_ask("yes") if book_ok else None
            no_ask = self.book.implied_ask("no") if book_ok else None
            ticker = self.book.market_ticker or self._market.get("ticker")
            market = self._market_for(ticker)
            close_at = parse_kxbtc15m_close(str(ticker)) if ticker else None
            seconds_left = None
            if close_at is not None:
                seconds_left = (close_at - datetime.now(timezone.utc)).total_seconds()
            strike = market.get("floor_strike")
            spot = self._brti.get("value")
            close_avg = self._brti.get("close_avg")
            close_window = self._brti.get("close_window")
            status = market.get("status")
            reference = self._market.get("ticker")
            signal = evaluate(
                spot=Decimal(str(spot)) if spot is not None else None,
                strike=Decimal(str(strike)) if strike is not None else None,
                seconds_left=seconds_left,
                close_avg=Decimal(str(close_avg)) if close_avg is not None else None,
                close_window=int(close_window) if close_window is not None else None,
                yes_ask=yes_ask,
                no_ask=no_ask,
                sigma=sigma_from_samples(list(self._spots)),
                book_valid=book_ok,
                data_fresh=self._data_fresh(time.monotonic()),
                market_status=str(status) if status else None,
                book_ticker=self.book.market_ticker,
                reference_ticker=str(reference) if reference else None,
            )
            payload = {
                "recorder": self._recorder_status(),
                "book_status": self._book_status,
                "error": self._error,
                "ticker": ticker,
                "close_at": close_at.isoformat() if close_at else None,
                "floor_strike": market.get("floor_strike"),
                "title": market.get("title"),
                "book": {
                    "valid": self.book.valid,
                    "yes": _levels(self.book.yes, reverse=True) if book_ok else [],
                    "no": _levels(self.book.no, reverse=True) if book_ok else [],
                    "yes_bid": _dec(yes_bid),
                    "no_bid": _dec(no_bid),
                    "yes_ask": _dec(yes_ask),
                    "no_ask": _dec(no_ask),
                },
                "brti": dict(self._brti),
                "signal": signal_to_dict(signal),
                "tape": list(self._tape),
                "streams": _stream_view(self._counts, rates),
                "last_row_at": self._last_row_at,
            }
        views = _session_views(self.session_dirs)
        primary = read_session_view(self.session_dir)
        payload["paper_sessions"] = views
        payload["paper_session"] = primary if primary is not None else (views[0] if views else None)
        return payload

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._replay_and_follow()
            except Exception as exc:  # noqa: BLE001 — keep the UI alive
                with self._lock:
                    self._error = str(exc)
                self._stop.wait(1.0)

    def _replay_and_follow(self) -> None:
        path = self.jsonl_path
        if not path.is_file():
            self._stop.wait(0.5)
            return
        start = _last_snapshot_offset(path)
        with self._lock:
            self.book.reset()
            self._counts.clear()
            self._rate_times.clear()
            self._tape.clear()
            self._spots.clear()
            self._book_status = "replaying" if start is not None else "waiting_snapshot"
            self._error = None
        offset = start if start is not None else path.stat().st_size
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            while not self._stop.is_set():
                pos = fh.tell()
                line = fh.readline()
                if line == "":
                    self._offset = pos
                    self._maybe_discover()
                    self._stop.wait(0.1)
                    if not path.is_file() or path.stat().st_size < pos:
                        return
                    continue
                if not line.endswith("\n"):
                    fh.seek(pos)
                    self._stop.wait(0.05)
                    continue
                self._apply_row(line)
                self._offset = fh.tell()

    def _apply_row(self, line: str) -> None:
        row = json.loads(line)
        stream = str(row.get("stream") or "unknown")
        payload = row.get("payload") or {}
        msg = payload.get("msg") or {}
        now = time.time()
        with self._lock:
            self._counts[stream] = self._counts.get(stream, 0) + 1
            self._rate_times.append((now, stream))
            self._last_row_at = str(row.get("local_received_at") or "")
            if stream == "orderbook_snapshot":
                previous = self.book.market_ticker
                result = self.book.apply_snapshot(payload)
                if result.ok:
                    self._book_status = "live"
                    self._book_mono = time.monotonic()
                    self._drop_close_if_ticker_changed(previous)
                    self._forget_stale_market()
                else:
                    self._book_status = "need_snapshot"
            elif stream == "orderbook_delta":
                previous = self.book.market_ticker
                result = self.book.apply_delta(payload)
                if result.ok:
                    self._book_status = "live"
                    self._book_mono = time.monotonic()
                    self._drop_close_if_ticker_changed(previous)
                    self._forget_stale_market()
                elif result.need_snapshot:
                    self._book_status = "need_snapshot"
            elif stream == "trade":
                self._tape.appendleft(
                    {
                        "ts_ms": row.get("ts_ms") or msg.get("ts_ms"),
                        "ticker": msg.get("market_ticker"),
                        "yes_price": msg.get("yes_price_dollars"),
                        "no_price": msg.get("no_price_dollars"),
                        "count": msg.get("count_fp"),
                        "taker_side": msg.get("taker_outcome_side") or msg.get("taker_side"),
                        "book_side": msg.get("taker_book_side"),
                    }
                )
            elif stream == "cfbenchmarks_value":
                self._brti = _brti_view(msg)
                self._brti_mono = time.monotonic()
                self._remember_spot(self._brti.get("value"), row.get("ts_ms") or msg.get("received_at"))
            elif stream == "cfbenchmarks_value_5hz":
                current = _brti_spot(msg)
                if current is not None:
                    self._brti["value"] = current
                    self._brti["ts_ms"] = row.get("ts_ms") or msg.get("received_at")

    def _market_for(self, ticker: str | None) -> dict[str, Any]:
        if ticker and self._market.get("ticker") == ticker:
            return self._market
        return {}

    def _forget_stale_market(self) -> None:
        ticker = self.book.market_ticker
        if not ticker or self._market.get("ticker") in (None, ticker):
            return
        self._market = {}
        self._last_discover = 0.0

    def _maybe_discover(self) -> None:
        if self.settings is None or not self.settings.has_prod_credentials:
            return
        now = time.monotonic()
        wanted = self.book.market_ticker
        stale = bool(wanted and self._market.get("ticker") not in (None, wanted))
        if not stale and now - self._last_discover < _DISCOVER_INTERVAL_S:
            return
        self._last_discover = now
        try:
            with KalshiReadClient(self.settings) as rest:
                summary = self._discover_summary(rest, wanted)
        except Exception:
            return
        if summary is None:
            return
        with self._lock:
            book_ticker = self.book.market_ticker
            if book_ticker and summary.get("ticker") != book_ticker:
                return
            self._market = summary

    def _discover_summary(self, rest: KalshiReadClient, wanted: str | None) -> dict[str, Any] | None:
        if wanted:
            raw_one = rest.get_market(wanted)
            market = raw_one.get("market") if isinstance(raw_one, dict) else None
            if not isinstance(market, dict):
                market = raw_one if isinstance(raw_one, dict) else None
            if not market or not market.get("ticker"):
                return None
            return summarize_market(market)
        raw = rest.get_markets(series_ticker=SERIES_TICKER_BTC_15M, status="open", limit=50)
        open_markets = [m for m in (raw.get("markets") or []) if is_currently_open(m)]
        open_markets.sort(key=lambda m: m.get("close_time") or "")
        if not open_markets:
            return None
        return summarize_market(open_markets[0])

    def _recorder_status(self) -> dict[str, Any]:
        pid = _lock_pid(self.lock_path)
        alive = pid is not None and _pid_alive(pid)
        stat = self.jsonl_path.stat() if self.jsonl_path.is_file() else None
        return {
            "lock_pid": pid,
            "running": alive,
            "jsonl": str(self.jsonl_path),
            "bytes": stat.st_size if stat else 0,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            if stat
            else None,
        }

    def _drop_close_if_ticker_changed(self, previous: str | None) -> None:
        current = self.book.market_ticker
        if not previous or not current or previous == current:
            return
        self._brti.pop("close_avg", None)
        self._brti.pop("close_window", None)

    def _data_fresh(self, now: float) -> bool:
        if self._book_mono is None or self._brti_mono is None:
            return False
        if now - self._book_mono > _QUOTE_MAX_AGE_S:
            return False
        if now - self._brti_mono > _QUOTE_MAX_AGE_S:
            return False
        return True

    def _remember_spot(self, value: Any, ts_ms: Any) -> None:
        if value is None:
            return
        stamp = int(ts_ms) if ts_ms is not None else None
        self._spots.append((stamp, Decimal(str(value))))

    def _rates(self, now: float) -> dict[str, float]:
        while self._rate_times and now - self._rate_times[0][0] > _RATE_WINDOW_S:
            self._rate_times.popleft()
        counts: dict[str, int] = {}
        for _, stream in self._rate_times:
            counts[stream] = counts.get(stream, 0) + 1
        window = _RATE_WINDOW_S
        return {name: round(count / window, 2) for name, count in sorted(counts.items())}


def _stream_view(counts: dict[str, int], rates: dict[str, float]) -> list[dict[str, Any]]:
    keys = sorted(counts)
    out: list[dict[str, Any]] = []
    for key in keys:
        out.append(
            {
                "id": key,
                "label": STREAM_LABELS.get(key, key),
                "count": counts[key],
                "per_sec": rates.get(key, 0.0),
            }
        )
    return out


def _brti_spot(msg: dict[str, Any]) -> str | None:
    raw = msg.get("data")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        value = parsed.get("value")
        return str(value) if value is not None else None
    return None


def _brti_view(msg: dict[str, Any]) -> dict[str, Any]:
    avg = msg.get("avg_60s_data") or {}
    close = msg.get("last_60s_windowed_average_15min") or {}
    return {
        "value": _brti_spot(msg),
        "ts_ms": msg.get("received_at"),
        "avg_60s": avg.get("value"),
        "avg_60s_window": avg.get("window_size"),
        "close_avg": close.get("value"),
        "close_window": close.get("window_size"),
    }


def _last_snapshot_offset(path: Path, *, max_bytes: int | None = _LOOKBACK_BYTES) -> int | None:
    size = path.stat().st_size
    floor = 0 if max_bytes is None else max(0, size - max_bytes)
    end = size
    found: int | None = None
    while end > floor:
        start = max(floor, end - _CHUNK_BYTES)
        with path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(end - start)
        if start > 0:
            nl = raw.find(b"\n")
            if nl < 0:
                end = start
                continue
            raw = raw[nl + 1 :]
            cursor = start + nl + 1
        else:
            cursor = 0
        pos = cursor
        for line in raw.split(b"\n"):
            if _is_snapshot_line(line):
                found = pos
            pos += len(line) + 1
        if found is not None:
            return found
        end = start
    return None


def _lock_pid(path: Path) -> int | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("pid="):
            return int(line.split("=", 1)[1])
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
