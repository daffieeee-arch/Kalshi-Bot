"""Offline last-minute model vs settlement on a production JSONL capture."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Never

from kalshi_bot.client import KalshiReadClient
from kalshi_bot.discover import parse_kxbtc15m_close
from kalshi_bot.orderbook import OrderbookState, Side
from kalshi_bot.paper import PaperIntent, PaperLedger
from kalshi_bot.signal import evaluate, realized_sigma

_SKIP_STREAMS = frozenset(
    {"trade", "cfbenchmarks_value_5hz", "subscribed", "ok", "unknown"}
)
_CONTRACTS = Decimal("1")
_SPOT_WINDOW = 90


@dataclass(frozen=True)
class MarketInfo:
    """Strike and official result from GET /markets/{ticker} or market_meta."""

    ticker: str
    floor_strike: Decimal | None
    result: Literal["yes", "no"] | None
    status: str


@dataclass
class WindowResult:
    """One KXBTC15M window after the last-minute paper rule."""

    ticker: str
    strike: Decimal | None
    close_avg: Decimal | None
    yes_won: bool | None
    official_result: Literal["yes", "no"] | None
    hint: str | None
    side: Side | None
    ask: Decimal | None
    model_yes: Decimal | None
    edge: Decimal | None
    n: int | None
    seconds_left: float | None
    pnl: Decimal | None
    skip: str | None


@dataclass
class ReplayReport:
    """Aggregate of settled last-minute paper decisions."""

    windows: list[WindowResult] = field(default_factory=list)

    @property
    def traded(self) -> list[WindowResult]:
        return [row for row in self.windows if row.side is not None and row.pnl is not None]

    @property
    def waits(self) -> int:
        return sum(1 for row in self.windows if row.skip == "wait")

    @property
    def pnl(self) -> Decimal:
        return sum((row.pnl or Decimal("0") for row in self.traded), Decimal("0"))

    @property
    def wins(self) -> int:
        return sum(1 for row in self.traded if (row.pnl or Decimal("0")) > 0)

    @property
    def losses(self) -> int:
        return sum(1 for row in self.traded if (row.pnl or Decimal("0")) < 0)

    def to_dict(self) -> dict[str, Any]:
        traded = self.traded
        return {
            "windows": len(self.windows),
            "traded": len(traded),
            "wins": self.wins,
            "losses": self.losses,
            "waits": self.waits,
            "pnl": format(self.pnl, "f"),
            "avg_pnl": format(self.pnl / len(traded), "f") if traded else None,
            "rows": [_window_dict(row) for row in self.windows],
        }


class _Window:
    def __init__(self, ticker: str, info: MarketInfo | None) -> None:
        self.ticker = ticker
        self.info = info
        self.book = OrderbookState()
        self.spots: deque[Decimal] = deque(maxlen=_SPOT_WINDOW)
        self.close_avg: Decimal | None = None
        self.close_window: int | None = None
        self.close_at = parse_kxbtc15m_close(ticker)
        self.decision: WindowResult | None = None
        self.ledger = PaperLedger()
        self.settled = False

    @property
    def strike(self) -> Decimal | None:
        if self.info is None:
            return None
        return self.info.floor_strike


def collect_tickers(path: Path) -> list[str]:
    """Unique book tickers in capture order. Skips the heavy delta stream."""
    seen: set[str] = set()
    out: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if '"orderbook_snapshot"' not in line and '"market_meta"' not in line:
                continue
            row = json.loads(line)
            stream = row.get("stream")
            payload = row.get("payload") or {}
            ticker = None
            if stream == "market_meta":
                ticker = payload.get("ticker")
            elif stream == "orderbook_snapshot":
                ticker = (payload.get("msg") or {}).get("market_ticker")
            if ticker and ticker not in seen:
                seen.add(str(ticker))
                out.append(str(ticker))
    return out


def fetch_markets(rest: KalshiReadClient, tickers: list[str]) -> dict[str, MarketInfo]:
    """GET /markets/{ticker} for floor_strike and official result."""
    out: dict[str, MarketInfo] = {}
    for ticker in tickers:
        raw = rest.get_market(ticker)
        market = raw.get("market") if isinstance(raw, dict) else None
        if not isinstance(market, dict):
            continue
        strike = market.get("floor_strike")
        raw_result = str(market.get("result") or "").lower()
        result: Literal["yes", "no"] | None
        if raw_result == "yes":
            result = "yes"
        elif raw_result == "no":
            result = "no"
        else:
            result = None
        out[ticker] = MarketInfo(
            ticker=ticker,
            floor_strike=Decimal(str(strike)) if strike is not None else None,
            result=result,
            status=str(market.get("status") or ""),
        )
    return out


def replay(
    path: Path,
    markets: Mapping[str, MarketInfo],
) -> ReplayReport:
    """Stream one JSONL capture and apply the live last-minute paper hint."""
    current: _Window | None = None
    report = ReplayReport()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stream = _stream_name(line)
            if stream in _SKIP_STREAMS:
                continue
            row = json.loads(line)
            stream = str(row.get("stream") or "unknown")
            if stream in _SKIP_STREAMS:
                continue
            payload = row.get("payload") or {}
            if stream == "market_meta":
                current = _roll(current, report, str(payload.get("ticker") or ""), markets)
                _apply_meta(current, payload, markets)
                continue
            if stream == "orderbook_snapshot":
                ticker = (payload.get("msg") or {}).get("market_ticker")
                if ticker:
                    current = _roll(current, report, str(ticker), markets)
                if current is not None:
                    current.book.apply_snapshot(payload)
                continue
            if stream == "orderbook_delta":
                if current is not None:
                    current.book.apply_delta(payload)
                continue
            if stream == "cfbenchmarks_value":
                if current is not None:
                    _on_brti(current, payload.get("msg") or {}, row.get("ts_ms"))
    if current is not None:
        report.windows.append(_finish(current))
    return report


def _roll(
    current: _Window | None,
    report: ReplayReport,
    ticker: str,
    markets: Mapping[str, MarketInfo],
) -> _Window:
    if current is not None and current.ticker == ticker:
        return current
    if current is not None:
        report.windows.append(_finish(current))
    return _Window(ticker, markets.get(ticker))


def _apply_meta(window: _Window, payload: dict[str, Any], markets: Mapping[str, MarketInfo]) -> None:
    strike = payload.get("floor_strike")
    if window.info is not None or strike is None:
        return
    extra = markets.get(window.ticker)
    window.info = MarketInfo(
        ticker=window.ticker,
        floor_strike=Decimal(str(strike)),
        result=extra.result if extra is not None else None,
        status=extra.status if extra is not None else "meta",
    )


def _on_brti(window: _Window, msg: dict[str, Any], ts_ms: Any) -> None:
    if window.settled:
        return
    view = _brti_view(msg)
    spot = view.get("value")
    if spot is not None:
        window.spots.append(Decimal(str(spot)))
    if view.get("close_avg") is not None:
        window.close_avg = Decimal(str(view["close_avg"]))
    if view.get("close_window") is not None:
        window.close_window = int(view["close_window"])
    seconds_left = _seconds_left(window, ts_ms)
    if window.close_window is not None and window.close_window >= 60:
        _settle(window)
        return
    if (
        window.decision is None
        and window.strike is not None
        and spot is not None
        and seconds_left is not None
        and seconds_left > 0
    ):
        signal = evaluate(
            spot=Decimal(str(spot)),
            strike=window.strike,
            seconds_left=seconds_left,
            close_avg=window.close_avg,
            close_window=window.close_window,
            yes_ask=window.book.implied_ask("yes"),
            no_ask=window.book.implied_ask("no"),
            sigma=realized_sigma(list(window.spots)),
        )
        if signal.regime == "last_minute":
            _maybe_take(window, signal, ts_ms)


def _maybe_take(window: _Window, signal: Any, ts_ms: Any) -> None:
    hint = signal.hint
    side: Side | None
    ask: Decimal | None
    edge: Decimal | None
    if hint == "paper YES?":
        side = "yes"
        ask = signal.yes_ask
        edge = signal.yes_edge
    elif hint == "paper NO?":
        side = "no"
        ask = signal.no_ask
        edge = signal.no_edge
    elif hint == "wait":
        return
    else:
        unreachable: str = hint
        raise ValueError(f"unknown hint {unreachable!r}")
    if side is None or ask is None:
        return
    intent = PaperIntent(
        market_ticker=window.ticker,
        outcome=side,
        price=ask,
        count=_CONTRACTS,
        style="taker",
    )
    window.ledger.add(intent)
    window.ledger.on_book(window.book, ts_ms=int(ts_ms) if ts_ms is not None else None)
    window.decision = WindowResult(
        ticker=window.ticker,
        strike=window.strike,
        close_avg=window.close_avg,
        yes_won=None,
        official_result=window.info.result if window.info else None,
        hint=hint,
        side=side,
        ask=ask,
        model_yes=signal.model_yes,
        edge=edge,
        n=window.close_window,
        seconds_left=signal.seconds_left,
        pnl=None,
        skip=None,
    )


def _settle(window: _Window) -> None:
    if window.settled:
        return
    strike = window.strike
    if window.close_avg is None or strike is None:
        return
    yes_won = _yes_won(window.info, window.close_avg, strike)
    if yes_won is None:
        return
    window.ledger.on_settlement(
        market_ticker=window.ticker,
        close_avg=window.close_avg,
        floor_strike=strike,
    )
    window.settled = True
    if window.decision is not None:
        intent = window.ledger.intents[0] if window.ledger.intents else None
        window.decision.yes_won = yes_won
        window.decision.close_avg = window.close_avg
        window.decision.pnl = intent.pnl if intent is not None else Decimal("0")
        if intent is not None and not intent.fills:
            window.decision.skip = "no_fill"
            window.decision.pnl = None


def _finish(window: _Window) -> WindowResult:
    if window.decision is not None:
        if window.decision.pnl is None and window.decision.skip is None:
            if not window.settled:
                window.decision.skip = "open"
            return window.decision
        return window.decision
    skip = "wait"
    if window.info is None or window.strike is None:
        skip = "no_strike"
    elif window.close_avg is None or window.close_window is None:
        skip = "no_close"
    elif not window.settled:
        skip = "open"
    return WindowResult(
        ticker=window.ticker,
        strike=window.strike,
        close_avg=window.close_avg,
        yes_won=_yes_won(window.info, window.close_avg, window.strike)
        if window.close_avg is not None and window.strike is not None
        else None,
        official_result=window.info.result if window.info else None,
        hint=None,
        side=None,
        ask=None,
        model_yes=None,
        edge=None,
        n=window.close_window,
        seconds_left=None,
        pnl=None,
        skip=skip,
    )


def _yes_won(
    info: MarketInfo | None,
    close_avg: Decimal | None,
    strike: Decimal | None,
) -> bool | None:
    result = info.result if info is not None else None
    if result == "yes":
        return True
    if result == "no":
        return False
    if result is None:
        if close_avg is None or strike is None:
            return None
        return close_avg >= strike
    unreachable: Never = result
    raise ValueError(f"unknown result {unreachable!r}")


def _seconds_left(window: _Window, ts_ms: Any) -> float | None:
    if window.close_at is None or ts_ms is None:
        return None
    now = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return (window.close_at - now).total_seconds()


def _stream_name(line: str) -> str:
    for key in ('"stream":"', '"stream": "'):
        start = line.find(key)
        if start < 0:
            continue
        start += len(key)
        end = line.find('"', start)
        if end > start:
            return line[start:end]
    return "unknown"


def _brti_view(msg: dict[str, Any]) -> dict[str, Any]:
    raw = msg.get("data")
    value = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        if parsed.get("value") is not None:
            value = str(parsed["value"])
    close = msg.get("last_60s_windowed_average_15min") or {}
    return {
        "value": value,
        "close_avg": close.get("value"),
        "close_window": close.get("window_size"),
    }


def _window_dict(row: WindowResult) -> dict[str, Any]:
    return {
        "ticker": row.ticker,
        "strike": None if row.strike is None else format(row.strike, "f"),
        "close_avg": None if row.close_avg is None else format(row.close_avg, "f"),
        "yes_won": row.yes_won,
        "official_result": row.official_result,
        "hint": row.hint,
        "side": row.side,
        "ask": None if row.ask is None else format(row.ask, "f"),
        "model_yes": None if row.model_yes is None else format(row.model_yes, "f"),
        "edge": None if row.edge is None else format(row.edge, "f"),
        "n": row.n,
        "seconds_left": None if row.seconds_left is None else round(row.seconds_left, 1),
        "pnl": None if row.pnl is None else format(row.pnl, "f"),
        "skip": row.skip,
    }
