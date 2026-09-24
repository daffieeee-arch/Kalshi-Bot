"""Paper-only session that tails the recorder JSONL. It never places orders.

One process appends ``data/prod-kxbtc15m.jsonl``. This process reads that file
and writes ``data/paper-sessions/`` only. KXBTC15M ``fee_type`` is ``quadratic``
with multiplier 1: taker fees come from ``fees.quadratic_taker_fee``, maker
fees are 0. A yes/no settlement pays $1 per winning contract and no settlement
fee. The +50% target is an aspirational KPI, not a forecast.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Never, Protocol, TextIO
from zoneinfo import ZoneInfo

from kalshi_bot.account import PaperAccount
from kalshi_bot.client import KalshiReadClient
from kalshi_bot.config import SERIES_TICKER_BTC_15M, Settings
from kalshi_bot.discover import parse_kxbtc15m_close
from kalshi_bot.exit_audit import (
    build_delta,
    build_quote,
    learner_influences,
    prediction_row,
    session_metrics,
    stamp_settlement,
)
from kalshi_bot.learn import MIN_INFLUENCE, OnlineLearner
from kalshi_bot.orderbook import OrderbookState, Side
from kalshi_bot.paper import (
    PaperFill,
    PaperIntent,
    PaperLedger,
    SettlementMark,
    net_open_qty,
    position_legs,
)
from kalshi_bot.recorder import brti_close_average_belongs, brti_settlement_matches
from kalshi_bot.signal import Signal, evaluate, sigma_from_samples
from kalshi_bot.strategy import (
    Adaptation,
    EntryPlan,
    StrategyParams,
    adapt,
    adapt_to_mark,
    cancel_reason,
    decide,
    decide_exit,
    initial_params,
    market_features,
    rolling_score_from_pnls,
)

log = logging.getLogger("kalshi_bot.session")

DEFAULT_SESSION_ROOT = Path("data/paper-sessions")
AB_REF_DIR = "ab-ref"
AB_NOLEARN_DIR = "ab-nolearn"
_AMS_TZ = ZoneInfo("Europe/Amsterdam")
_OFFICIAL_WAIT = timedelta(seconds=180)
_POLL_EVERY = timedelta(seconds=15)
_QUOTE_MAX_AGE_S = 5.0
_SPOT_WINDOW = 90
_CHUNK_BYTES = 4_000_000
_SNAP_NEEDLES = (b'"stream":"orderbook_snapshot"', b'"stream": "orderbook_snapshot"')
_META_NEEDLES = (b'"stream":"market_meta"', b'"stream": "market_meta"')
_VIEW_FILLS = 40
_VIEW_CLOSED = 40
_VIEW_ADAPTATIONS = 12
_MARK_ADAPT_S = 30.0

OfficialResult = Literal["yes", "no"]


class MarketLookup(Protocol):
    """Read-only market fields. Implementations must not send orders."""

    def official_result(self, ticker: str) -> OfficialResult | None: ...

    def floor_strike(self, ticker: str) -> Decimal | None: ...


@dataclass
class PendingSettle:
    """A closed window waiting for the official result."""

    ticker: str
    strike: Decimal | None
    close_avg: Decimal | None
    deadline: datetime
    next_poll: datetime | None = None


@dataclass
class LabelWatch:
    """A traded ticker still waiting for a settlement label. This does not change PnL."""

    ticker: str
    deadline: datetime
    next_poll: datetime | None = None


class SessionLock:
    """One paper session per host. A second process must not double-trade."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError(f"paper session lock is held: {self.path}") from exc
        self._fh.write(f"pid={os.getpid()}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


class PaperSession:
    """Bankroll, strategy, and fills for one timed paper run."""

    def __init__(
        self,
        root: Path,
        *,
        session_id: str,
        started_at: datetime,
        ends_at: datetime,
        bankroll: Decimal,
        target_return: Decimal,
        account: PaperAccount,
        params: StrategyParams,
        learn_exit_orders: bool = True,
        arm: str | None = None,
        min_closes: int | None = None,
        drawdown_stop: Decimal | None = None,
    ) -> None:
        self.root = root
        self.session_id = session_id
        self.started_at = started_at
        self.ends_at = ends_at
        self.bankroll = bankroll
        self.target_return = target_return
        self.account = account
        self.params = params
        self.learn_exit_orders = learn_exit_orders
        self.arm = _arm_for(learn_exit_orders, arm)
        self.min_closes = min_closes
        self.drawdown_stop = drawdown_stop
        self.finished = False
        self.finished_known = True
        self.stop_reason: str | None = None
        self.max_open = 0
        self.equity_peak = bankroll
        self.max_drawdown = Decimal("0")
        self.predictions: list[dict[str, Any]] = []
        self.label_watch: list[LabelWatch] = []
        self.learner = OnlineLearner.cold()
        self.adaptations: list[tuple[str, Adaptation]] = []
        self.last_entry_ms: int | None = None
        self.settled_at_adapt = 0
        self.mark_guard: str | None = None
        self._closes_since_loosen = 0
        self._last_mark_adapt_mono: float | None = None
        self.jsonl_offset = 0
        self.book = OrderbookState()
        self.spots: deque[tuple[int | None, Decimal]] = deque(maxlen=_SPOT_WINDOW)
        self.spot: Decimal | None = None
        self.ticker: str | None = None
        self.strike: Decimal | None = None
        self.close_avg: Decimal | None = None
        self.close_window: int | None = None
        self.pending: list[PendingSettle] = []
        self.lookup: MarketLookup | None = None
        self.armed = False
        self._fresh = False
        self._stop = threading.Event()
        self._book_mono: float | None = None
        self._brti_mono: float | None = None
        self._strike_miss: set[str] = set()
        self._last_save_mono = 0.0

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        bankroll: Decimal,
        hours: float,
        target_return: Decimal,
        now: datetime,
        learn_exit_orders: bool = True,
        min_closes: int | None = None,
        drawdown_stop: Decimal | None = None,
    ) -> PaperSession:
        if bankroll <= 0:
            raise ValueError("bankroll must be positive")
        if hours <= 0:
            raise ValueError("hours must be positive")
        if target_return < 0:
            raise ValueError("target_return must be >= 0")
        session_id = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        session = cls(
            root,
            session_id=session_id,
            started_at=now,
            ends_at=now + timedelta(hours=hours),
            bankroll=bankroll,
            target_return=target_return,
            account=PaperAccount(bankroll),
            params=initial_params(bankroll),
            learn_exit_orders=learn_exit_orders,
            min_closes=min_closes,
            drawdown_stop=drawdown_stop,
        )
        session._fresh = True
        return session

    def request_stop(self) -> None:
        self._stop.set()

    def catch_up(self, path: Path) -> None:
        """Build the book from the capture. A new session does not trade history."""
        if not path.is_file():
            self.armed = True
            self._fresh = False
            return
        size = path.stat().st_size
        if self._fresh:
            boundary = size
            live_from: int | None = None
        else:
            boundary = min(max(self.jsonl_offset, 0), size)
            live_from = boundary
        meta_at = _last_offset_before(path, _META_NEEDLES, boundary)
        snap_at = _last_offset_before(path, _SNAP_NEEDLES, boundary)
        if meta_at is not None:
            self._apply_quiet(_line_at(path, meta_at))
        if snap_at is not None:
            self._replay(path, snap_at, boundary, live=False)
        if live_from is not None and live_from < size:
            self.armed = True
            self._replay(path, live_from, size, live=True)
        self.jsonl_offset = size
        self.armed = True
        self._fresh = False

    def run_loop(self, path: Path) -> int:
        self.catch_up(path)
        self.save()
        stop_reason: str | None = None
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            if self._drawdown_hit():
                stop_reason = "drawdown"
                log.warning(
                    "paper session %s drawdown from bankroll exceeded %s",
                    self.session_id,
                    format(self.drawdown_stop or Decimal("0"), "f"),
                )
                break
            if self._schedule_done(now):
                stop_reason = "schedule"
                log.info("paper session %s reached %s", self.session_id, _ams(self.ends_at))
                break
            try:
                self._pump(path)
                self.poll_official(now)
                self._maybe_mark_adapt(now)
                if time.monotonic() - self._last_save_mono >= 1.0:
                    self.save()
            except Exception as exc:  # noqa: BLE001 — a 24h loop must survive one bad line
                log.exception("paper loop error: %s", exc)
                if self._stop.wait(1.0):
                    break
                continue
            if self._stop.wait(0.1):
                break
        if stop_reason is not None:
            self.finished = True
            self.stop_reason = stop_reason
        self.save()
        return 0

    def apply_live_row(self, row: dict[str, Any], *, now: datetime | None = None) -> None:
        self._apply_live(row, now or datetime.now(timezone.utc))

    def poll_official(self, now: datetime) -> None:
        if not self.armed:
            return
        self._queue_closed(now)
        still: list[PendingSettle] = []
        for item in self.pending:
            if not _has_open(self.account.ledger, item.ticker):
                continue
            official = self._poll_result(item, now)
            strike = item.strike if item.strike is not None else self._strike_for(item.ticker)
            if official is not None:
                self.settle_ticker(
                    item.ticker,
                    official=official,
                    close_avg=item.close_avg,
                    strike=strike,
                    now=now,
                )
                continue
            if now >= item.deadline and item.close_avg is not None and strike is not None:
                log.info("settling %s from reconstructed BRTI; official result not in yet", item.ticker)
                self.settle_ticker(
                    item.ticker,
                    official=None,
                    close_avg=item.close_avg,
                    strike=strike,
                    now=now,
                )
                continue
            still.append(item)
        self.pending = still
        self._poll_labels(now)

    def settle_ticker(
        self,
        ticker: str,
        *,
        official: OfficialResult | None,
        close_avg: Decimal | None,
        strike: Decimal | None,
        now: datetime | None = None,
    ) -> SettlementMark | None:
        watched = [
            intent
            for intent in self.account.ledger.intents
            if intent.market_ticker == ticker and intent.status == "open"
        ]
        mark = self.account.ledger.on_settlement(
            market_ticker=ticker,
            close_avg=close_avg,
            floor_strike=strike,
            official_result=official,
        )
        if mark is None:
            return None
        paid = self.account.credit_settlements()
        log.info(
            "settled %s source=%s yes_won=%s payout=%s mismatch=%s",
            ticker,
            mark.source,
            mark.yes_won,
            format(paid, "f"),
            mark.mismatch,
        )
        moment = now or datetime.now(timezone.utc)
        stamp = int(moment.timestamp() * 1000)
        for intent in watched:
            if intent.status == "settled":
                learned = self.learner.wants_exit(intent.entry_features) if intent.entry_features else False
                self._record_exit(intent, None, stamp, intent.exit_reason or "settlement", learned)
                self._learn(intent, intent.exit_reason or "settlement")
        self._apply_settlement_label(ticker, mark.yes_won, mark.source)
        self._maybe_adapt(moment)
        self.save()
        return mark

    def save(self) -> None:
        directory = self.root / self.session_id
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        payload = self.to_payload(now)
        _atomic_json(directory / "state.json", payload)
        metrics = payload.get("view", {}).get("metrics")
        if isinstance(metrics, dict):
            _atomic_json(directory / "metrics.json", metrics)
        _atomic_json(self.root / "current.json", {"session_id": self.session_id, "arm": self.arm})
        self._last_save_mono = time.monotonic()

    def to_payload(self, now: datetime) -> dict[str, Any]:
        self._track_equity()
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "bankroll": format(self.bankroll, "f"),
            "target_return": format(self.target_return, "f"),
            "cash": format(self.account.cash, "f"),
            "params": self.params.to_dict(),
            "adaptations": [
                {
                    "at": at,
                    "reason": item.reason,
                    "before": item.before.to_dict(),
                    "after": item.after.to_dict(),
                }
                for at, item in self.adaptations
            ],
            "last_entry_ms": self.last_entry_ms,
            "settled_at_adapt": self.settled_at_adapt,
            "learn_exit_orders": self.learn_exit_orders,
            "arm": self.arm,
            "min_closes": self.min_closes,
            "drawdown_stop": _num(self.drawdown_stop),
            "finished": self.finished,
            "stop_reason": self.stop_reason,
            "equity_peak": format(self.equity_peak, "f"),
            "max_drawdown": format(self.max_drawdown, "f"),
            "max_open": self.max_open,
            "predictions": self.predictions,
            "label_watch": [
                {"ticker": item.ticker, "deadline": item.deadline.isoformat()}
                for item in self.label_watch
            ],
            "learner": self.learner.to_dict(),
            "mark_guard": self.mark_guard,
            "closes_since_loosen": self._closes_since_loosen,
            "jsonl_offset": self.jsonl_offset,
            "taken": [
                {
                    "ticker": ticker,
                    "side": side,
                    "price": format(price, "f"),
                    "qty": format(qty, "f"),
                }
                for ticker, side, price, qty in self.account.ledger.export_taken()
            ],
            "intents": [_intent_dict(intent) for intent in self.account.ledger.intents],
            "pending": [
                {
                    "ticker": item.ticker,
                    "strike": _num(item.strike),
                    "close_avg": _num(item.close_avg),
                    "deadline": item.deadline.isoformat(),
                }
                for item in self.pending
            ],
            "view": self.view(now),
        }

    def view(self, now: datetime) -> dict[str, Any]:
        equity = self.account.equity(self.book)
        realized = self.account.realized_pnl()
        unrealized = self.account.unrealized_pnl(self.book)
        wins, losses = self.account.win_loss()
        decided = wins + losses
        goal = self.bankroll * self.target_return
        progress = None if goal <= 0 else (equity - self.bankroll) / goal
        seconds_left = (self.ends_at - now).total_seconds()
        return {
            "session_id": self.session_id,
            "trade_env": "paper",
            "arm": self.arm,
            "arm_label": _arm_label(self.arm),
            "learn_exit_orders": self.learn_exit_orders,
            "learner_influences": learner_influences(learn_exit_orders=self.learn_exit_orders),
            "strategy": self.params.name,
            "target_return": format(self.target_return, "f"),
            "target_note": "Aspirational KPI only. Not a forecast or a guarantee.",
            "bankroll": format(self.bankroll, "f"),
            "cash": format(self.account.cash, "f"),
            "equity": format(equity, "f"),
            "realized_pnl": format(realized, "f"),
            "unrealized_pnl": format(unrealized, "f"),
            "wins": wins,
            "losses": losses,
            "win_rate": None if decided == 0 else format(Decimal(wins) / Decimal(decided), "f"),
            "progress": None if progress is None else format(progress, "f"),
            "started_at": self.started_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "started_at_amsterdam": _ams(self.started_at),
            "ends_at_amsterdam": _ams(self.ends_at),
            "updated_at": now.isoformat(),
            "updated_at_amsterdam": _ams(now),
            "seconds_left": max(0.0, seconds_left),
            "params": self.params.to_dict(),
            "adaptations": _adaptation_rows(self.adaptations),
            "learner": self.learner.summary(),
            "open_trades": _trade_rows(self.account.ledger, statuses=("open",), book=self.book),
            "closed_trades": _trade_rows(
                self.account.ledger,
                statuses=("settled", "closed"),
                book=self.book,
            )[:_VIEW_CLOSED],
            "fills": _fill_rows(self.account.ledger)[:_VIEW_FILLS],
            "min_closes": self.min_closes,
            "drawdown_stop": _num(self.drawdown_stop),
            "finished": self.finished,
            "stop_reason": self.stop_reason,
            "metrics": self._metrics(now),
            "fee_model": (
                "quadratic taker 0.07*C*P*(1-P) rounded up to $0.000001; "
                "maker 0; in-window sells pay the same taker fee; "
                "yes/no settlement $1 per winning contract, fee 0"
            ),
        }

    @classmethod
    def load(cls, root: Path) -> PaperSession | None:
        pointer = root / "current.json"
        if not pointer.is_file():
            return None
        meta = json.loads(pointer.read_text(encoding="utf-8"))
        session_id = str(meta["session_id"])
        state_path = root / session_id / "state.json"
        if not state_path.is_file():
            return None
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return cls.from_payload(root, payload)

    @classmethod
    def from_payload(cls, root: Path, payload: dict[str, Any]) -> PaperSession:
        bankroll = Decimal(str(payload["bankroll"]))
        ledger = PaperLedger()
        for raw in payload.get("intents") or []:
            ledger.add(_intent_from(raw))
        ledger.import_taken(
            [
                (row["ticker"], row["side"], Decimal(str(row["price"])), Decimal(str(row["qty"])))
                for row in payload.get("taken") or []
            ]
        )
        account = PaperAccount(bankroll, ledger)
        account.cash = Decimal(str(payload["cash"]))
        account.mark_credited()
        if "learn_exit_orders" in payload:
            learn_exit_orders = bool(payload["learn_exit_orders"])
            arm = str(payload.get("arm") or ("ref" if learn_exit_orders else "no_learn_exit"))
            finished_known = "finished" in payload
        else:
            learn_exit_orders = True
            arm = "legacy"
            finished_known = False
        raw_stop = payload.get("drawdown_stop")
        drawdown_stop = None if raw_stop in (None, "") else Decimal(str(raw_stop))
        raw_min = payload.get("min_closes")
        min_closes = None if raw_min is None else int(raw_min)
        session = cls(
            root,
            session_id=str(payload["session_id"]),
            started_at=_parse_dt(str(payload["started_at"])),
            ends_at=_parse_dt(str(payload["ends_at"])),
            bankroll=bankroll,
            target_return=Decimal(str(payload["target_return"])),
            account=account,
            params=StrategyParams.from_dict(payload["params"]),
            learn_exit_orders=learn_exit_orders,
            arm=arm,
            min_closes=min_closes,
            drawdown_stop=drawdown_stop,
        )
        session.finished_known = finished_known
        session.finished = bool(payload.get("finished")) if finished_known else False
        session.stop_reason = payload.get("stop_reason")
        session.equity_peak = Decimal(str(payload.get("equity_peak") or payload["bankroll"]))
        session.max_drawdown = Decimal(str(payload.get("max_drawdown") or "0"))
        session.max_open = int(payload.get("max_open") or 0)
        predictions = payload.get("predictions") or []
        session.predictions = [row for row in predictions if isinstance(row, dict)]
        session.label_watch = [
            LabelWatch(ticker=str(row["ticker"]), deadline=_parse_dt(str(row["deadline"])))
            for row in payload.get("label_watch") or []
            if isinstance(row, dict) and row.get("ticker") and row.get("deadline")
        ]
        session.last_entry_ms = payload.get("last_entry_ms")
        session.settled_at_adapt = int(payload.get("settled_at_adapt") or 0)
        session.learner = OnlineLearner.from_dict(payload.get("learner"))
        session.mark_guard = payload.get("mark_guard")
        session._closes_since_loosen = int(payload.get("closes_since_loosen") or 0)
        session.jsonl_offset = int(payload.get("jsonl_offset") or 0)
        session.adaptations = [
            (
                str(row["at"]),
                Adaptation(
                    reason=str(row["reason"]),
                    before=StrategyParams.from_dict(row["before"]),
                    after=StrategyParams.from_dict(row["after"]),
                ),
            )
            for row in payload.get("adaptations") or []
        ]
        session.pending = [
            PendingSettle(
                ticker=str(row["ticker"]),
                strike=_dec(row.get("strike")),
                close_avg=_dec(row.get("close_avg")),
                deadline=_parse_dt(str(row["deadline"])),
            )
            for row in payload.get("pending") or []
        ]
        session.armed = True
        return session

    def _pump(self, path: Path) -> None:
        if not path.is_file():
            return
        size = path.stat().st_size
        if size < self.jsonl_offset:
            log.warning("jsonl shrank; following the new end without replaying history")
            self.jsonl_offset = size
            return
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(self.jsonl_offset)
            while not self._stop.is_set():
                pos = fh.tell()
                line = fh.readline()
                if line == "":
                    self.jsonl_offset = pos
                    return
                if not line.endswith("\n"):
                    fh.seek(pos)
                    return
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skipping corrupt jsonl line at %s", pos)
                    self.jsonl_offset = fh.tell()
                    continue
                self._apply_live(row, datetime.now(timezone.utc))
                self.jsonl_offset = fh.tell()

    def _replay(self, path: Path, start: int, end: int, *, live: bool) -> None:
        if start >= end:
            return
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(start)
            while fh.tell() < end and not self._stop.is_set():
                line = fh.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    break
                row = json.loads(line)
                if live:
                    self._apply_live(row, datetime.now(timezone.utc))
                else:
                    self._apply_quiet(row)

    def _apply_quiet(self, row: dict[str, Any]) -> None:
        stream = str(row.get("stream") or "")
        payload = row.get("payload") or {}
        ts_ms = _as_int(row.get("ts_ms"))
        if stream == "market_meta":
            self._set_meta(payload, live=False, now=self.started_at)
        elif stream == "orderbook_snapshot":
            self._on_snapshot(payload, live=False, ts_ms=ts_ms, now=self.started_at)
        elif stream == "orderbook_delta":
            self._on_delta(payload, live=False, ts_ms=ts_ms, now=self.started_at)
        elif stream == "cfbenchmarks_value":
            self._on_brti(payload.get("msg") or {}, ts_ms=ts_ms, live=False, now=self.started_at)

    def _apply_live(self, row: dict[str, Any], now: datetime) -> None:
        stream = str(row.get("stream") or "")
        payload = row.get("payload") or {}
        ts_ms = _as_int(row.get("ts_ms"))
        if stream == "market_meta":
            self._set_meta(payload, live=True, now=now)
        elif stream == "orderbook_snapshot":
            self._on_snapshot(payload, live=True, ts_ms=ts_ms, now=now)
        elif stream == "orderbook_delta":
            self._on_delta(payload, live=True, ts_ms=ts_ms, now=now)
        elif stream == "trade":
            self._note(self.account.ledger.on_trade(payload))
            self._on_quote(ts_ms, now)
        elif stream == "cfbenchmarks_value":
            self._on_brti(payload.get("msg") or {}, ts_ms=ts_ms, live=True, now=now)

    def _set_meta(self, payload: dict[str, Any], *, live: bool, now: datetime) -> None:
        ticker = payload.get("ticker")
        if not ticker:
            return
        nxt = str(ticker)
        old = self.ticker
        old_strike = self.strike
        if old is not None and nxt != old:
            self.close_avg = None
            self.close_window = None
            if live:
                self._watch(old, old_strike, None, now)
        self.ticker = nxt
        raw_strike = payload.get("floor_strike")
        if raw_strike is not None:
            self.strike = Decimal(str(raw_strike))

    def _on_snapshot(self, payload: dict[str, Any], *, live: bool, ts_ms: int | None, now: datetime) -> None:
        previous = self.book.market_ticker
        result = self.book.apply_snapshot(payload)
        if not result.ok:
            return
        self._book_mono = time.monotonic()
        if previous and self.book.market_ticker and previous != self.book.market_ticker:
            self.close_avg = None
            self.close_window = None
        if live:
            self._note(self.account.ledger.on_book(self.book, ts_ms=ts_ms))
            self._on_quote(ts_ms, now)

    def _on_delta(self, payload: dict[str, Any], *, live: bool, ts_ms: int | None, now: datetime) -> None:
        previous = self.book.market_ticker
        result = self.book.apply_delta(payload)
        if not result.ok:
            return
        self._book_mono = time.monotonic()
        if previous and self.book.market_ticker and previous != self.book.market_ticker:
            self.close_avg = None
            self.close_window = None
        if live:
            self._note(self.account.ledger.on_book(self.book, ts_ms=ts_ms))
            self._on_quote(ts_ms, now)

    def _on_brti(
        self,
        msg: dict[str, Any],
        *,
        ts_ms: int | None,
        live: bool,
        now: datetime,
    ) -> None:
        spot = _brti_spot(msg)
        if spot is not None:
            self.spot = spot
            self.spots.append((ts_ms, spot))
            self._brti_mono = time.monotonic()
        close = msg.get("last_60s_windowed_average_15min") or {}
        if isinstance(close, dict) and close.get("value") is not None and self.ticker:
            if brti_close_average_belongs(self.ticker, close, event_ts_ms=ts_ms):
                self.close_avg = Decimal(str(close["value"]))
                if close.get("window_size") is not None:
                    self.close_window = int(close["window_size"])
            if live and brti_settlement_matches(self.ticker, close, event_ts_ms=ts_ms):
                self._watch(self.ticker, self.strike, Decimal(str(close["value"])), now)
            if live:
                for item in self.pending:
                    if item.ticker == self.ticker:
                        continue
                    if brti_settlement_matches(item.ticker, close, event_ts_ms=ts_ms):
                        self._watch(item.ticker, item.strike, Decimal(str(close["value"])), now)
        if live:
            self._on_quote(ts_ms, now)

    def _on_quote(self, ts_ms: int | None, now: datetime) -> None:
        if not self.armed or now >= self.ends_at or not self.ticker:
            return
        self._ensure_strike()
        seconds_left = _seconds_left(self.ticker, now)
        book_ok = self.book.valid
        signal = evaluate(
            spot=self.spot,
            strike=self.strike,
            seconds_left=seconds_left,
            close_avg=self.close_avg,
            close_window=self.close_window,
            yes_ask=self.book.implied_ask("yes") if book_ok else None,
            no_ask=self.book.implied_ask("no") if book_ok else None,
            sigma=sigma_from_samples(list(self.spots)),
            book_valid=book_ok,
            data_fresh=self._fresh_quotes(),
            book_ticker=self.book.market_ticker,
            reference_ticker=self.ticker,
        )
        stamp = ts_ms if ts_ms is not None else int(now.timestamp() * 1000)
        self._stamp_open_entries(signal, stamp)
        self._cancel_wrong(signal)
        self._exit_open(signal, stamp, now)
        plan = self._entry_plan(signal, stamp)
        if plan is None:
            return
        features = market_features(signal, self.book, plan.outcome, plan.style, plan.limit)
        self._submit(plan, ts_ms, features, signal, stamp)

    def _entry_plan(self, signal: Signal, stamp: int) -> EntryPlan | None:
        if self.ticker is None:
            return None
        plan = self._decide(signal, self.params, stamp)
        if plan is None or self.learner.n < MIN_INFLUENCE:
            return plan
        features = market_features(signal, self.book, plan.outcome, plan.style, plan.limit)
        tuned = self.learner.adjust_params(self.params, features)
        if tuned == self.params:
            return plan
        revised = self._decide(signal, tuned, stamp)
        if revised is None:
            return None
        features = market_features(signal, self.book, revised.outcome, revised.style, revised.limit)
        tuned = self.learner.adjust_params(self.params, features)
        return self._decide(signal, tuned, stamp)

    def _decide(self, signal: Signal, params: StrategyParams, stamp: int) -> EntryPlan | None:
        if self.ticker is None:
            return None
        return decide(
            signal,
            params,
            self.book,
            available_cash=self.account.available_cash(),
            open_risk=self.account.open_risk(),
            now_ms=stamp,
            last_entry_ms=self.last_entry_ms,
            close_window=self.close_window,
            ticker_busy=_ticker_busy(self.account.ledger, self.ticker),
        )

    def _cancel_wrong(self, signal: Signal) -> None:
        if self.ticker is None:
            return
        for intent in self.account.ledger.intents:
            if intent.status != "open" or intent.market_ticker != self.ticker or intent.remaining <= 0:
                continue
            reason = cancel_reason(
                signal,
                self.params,
                outcome=intent.outcome,
                style=intent.style,
                price=intent.price,
            )
            if reason is None:
                continue
            if not self.account.ledger.cancel_resting(intent, reason):
                continue
            log.info(
                "paper cancel %s %s %s remaining was resting",
                reason,
                intent.outcome,
                intent.market_ticker,
            )
            self.save()

    def _exit_open(self, signal: Signal, stamp: int, now: datetime) -> None:
        if self.ticker is None:
            return
        for intent in self.account.ledger.intents:
            if intent.status != "open" or intent.market_ticker != self.ticker:
                continue
            qty = net_open_qty(intent)
            if qty <= 0:
                continue
            bought, _sold, buy_cost, *_rest = position_legs(intent)
            avg = buy_cost / bought if bought > 0 else intent.price
            features = market_features(signal, self.book, intent.outcome, intent.style, avg)
            held = None if intent.opened_ms is None else stamp - intent.opened_ms
            learned_signal = self.learner.wants_exit(features)
            plan = decide_exit(
                signal,
                self.params,
                self.book,
                outcome=intent.outcome,
                filled=qty,
                avg_price=avg,
                learn_exit=learned_signal and self.learn_exit_orders,
                held_ms=held,
            )
            if plan is None:
                continue
            cancelled = self.account.ledger.cancel_resting(intent, plan.reason)
            fills = self.account.ledger.sell_open(
                intent,
                self.book,
                ts_ms=stamp,
                min_price=plan.min_price,
                reason=plan.reason,
                slippage=plan.slippage,
            )
            self._note(fills)
            if not fills:
                if cancelled:
                    self.save()
                continue
            log.info(
                "paper exit %s %s %s reason %s (%s)",
                intent.outcome,
                format(sum((fill.count for fill in fills), Decimal("0")), "f"),
                intent.market_ticker,
                plan.reason,
                "flat" if intent.status == "closed" else "partial",
            )
            if intent.status != "closed":
                continue
            self._record_exit(intent, signal, stamp, plan.reason, learned_signal)
            self.last_entry_ms = stamp
            self._learn(intent, plan.reason)
            self._maybe_adapt(now)
            self.save()

    def _submit(
        self,
        plan: EntryPlan,
        ts_ms: int | None,
        features: list[float],
        signal: Signal,
        stamp: int,
    ) -> None:
        if self.ticker is None:
            return
        intent = PaperIntent(
            market_ticker=self.ticker,
            outcome=plan.outcome,
            price=plan.limit,
            count=plan.count,
            style=plan.style,
        )
        intent.entry_features = features
        self.account.ledger.add(intent)
        self.last_entry_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        if plan.style == "taker":
            self._note(self.account.ledger.on_book(self.book, ts_ms=ts_ms))
            intent.remaining = Decimal("0")
            if not intent.fills:
                self.account.ledger.intents.pop()
                return
            self._stamp_entry(intent, signal, stamp)
        elif plan.style == "maker":
            pass
        else:
            unreachable: Never = plan.style
            raise ValueError(f"unknown style {unreachable!r}")
        log.info(
            "paper %s %s %s %s limit %s edge %s",
            plan.style,
            plan.outcome,
            format(plan.count, "f"),
            self.ticker,
            format(plan.limit, "f"),
            format(plan.edge, "f"),
        )
        self.save()

    def _note(self, fills: list[PaperFill]) -> None:
        if not fills:
            return
        self.account.note_fills(fills)
        self.save()

    def _ensure_strike(self) -> None:
        if self.strike is not None or self.ticker is None or self.lookup is None:
            return
        if self.ticker in self._strike_miss:
            return
        strike = self.lookup.floor_strike(self.ticker)
        if strike is None:
            self._strike_miss.add(self.ticker)
            return
        self.strike = strike

    def _fresh_quotes(self) -> bool:
        if self._book_mono is None or self._brti_mono is None:
            return False
        now = time.monotonic()
        return (now - self._book_mono) <= _QUOTE_MAX_AGE_S and (now - self._brti_mono) <= _QUOTE_MAX_AGE_S

    def _queue_closed(self, now: datetime) -> None:
        seen: set[str] = set()
        for intent in self.account.ledger.intents:
            if intent.status != "open":
                continue
            ticker = intent.market_ticker
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            close_at = parse_kxbtc15m_close(ticker)
            if close_at is not None and close_at <= now:
                strike = self.strike if ticker == self.ticker else self._pending_strike(ticker)
                close_avg = self.close_avg if ticker == self.ticker else self._pending_close(ticker)
                self._watch(ticker, strike, close_avg, now)

    def _watch(
        self,
        ticker: str,
        strike: Decimal | None,
        close_avg: Decimal | None,
        now: datetime,
    ) -> None:
        for item in self.pending:
            if item.ticker != ticker:
                continue
            if strike is not None:
                item.strike = strike
            if close_avg is not None:
                item.close_avg = close_avg
            return
        close_at = parse_kxbtc15m_close(ticker)
        deadline = (close_at + _OFFICIAL_WAIT) if close_at is not None else (now + _OFFICIAL_WAIT)
        self.pending.append(
            PendingSettle(ticker=ticker, strike=strike, close_avg=close_avg, deadline=deadline)
        )

    def _poll_result(self, item: PendingSettle, now: datetime) -> OfficialResult | None:
        if self.lookup is None:
            return None
        if item.next_poll is not None and now < item.next_poll:
            return None
        item.next_poll = now + _POLL_EVERY
        return self.lookup.official_result(item.ticker)

    def _maybe_adapt(self, now: datetime) -> None:
        """React to each close. Loosen at most once per four closes."""
        self.mark_guard = None
        pnls = _closed_pnls(self.account.ledger)
        self._closes_since_loosen += 1
        change = adapt(
            self.params,
            score=rolling_score_from_pnls(pnls),
            equity=self.account.equity(self.book),
            bankroll=self.bankroll,
            target_return=self.target_return,
            elapsed_s=(now - self.started_at).total_seconds(),
            duration_s=(self.ends_at - self.started_at).total_seconds(),
            allow_loosen=self._closes_since_loosen >= 4,
        )
        self.settled_at_adapt = len(pnls)
        if change is None:
            return
        if change.reason.startswith("loosen"):
            self._closes_since_loosen = 0
        self.params = change.after
        self.adaptations.append((now.isoformat(), change))
        log.info("%s", change.reason)

    def _maybe_mark_adapt(self, now: datetime) -> None:
        """At most one tighten per open position when the live mark is down."""
        if not self.armed:
            return
        mono = time.monotonic()
        if self._last_mark_adapt_mono is not None and mono - self._last_mark_adapt_mono < _MARK_ADAPT_S:
            return
        self._last_mark_adapt_mono = mono
        key = _open_guard_key(self.account.ledger)
        if key is None or self.mark_guard == key:
            return
        drawdown = max(Decimal("1"), (self.bankroll * Decimal("0.005")).quantize(Decimal("0.01")))
        change = adapt_to_mark(
            self.params,
            unrealized=self.account.unrealized_pnl(self.book),
            drawdown=drawdown,
        )
        if change is None:
            return
        self.mark_guard = key
        self.params = change.after
        self.adaptations.append((now.isoformat(), change))
        log.info("%s", change.reason)
        self.save()

    def _schedule_done(self, now: datetime) -> bool:
        """Stop after the clock, and after ``min_closes`` when that floor is set."""
        if now < self.ends_at:
            return False
        if self.min_closes is None:
            return True
        return self._close_count() >= self.min_closes

    def _drawdown_hit(self) -> bool:
        if self.drawdown_stop is None:
            return False
        self._track_equity()
        dd = self.bankroll - self.account.equity(self.book)
        if dd < 0:
            return False
        return dd > self.drawdown_stop

    def _close_count(self) -> int:
        return sum(
            1
            for intent in self.account.ledger.intents
            if intent.status in ("closed", "settled") and intent.fills and intent.pnl is not None
        )

    def _track_equity(self) -> None:
        equity = self.account.equity(self.book)
        if equity > self.equity_peak:
            self.equity_peak = equity
        drawdown = self.equity_peak - equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
        open_n = sum(
            1
            for intent in self.account.ledger.intents
            if intent.status == "open" and net_open_qty(intent) > 0
        )
        if open_n > self.max_open:
            self.max_open = open_n

    def _metrics(self, now: datetime) -> dict[str, Any]:
        self._track_equity()
        return session_metrics(
            self.account.ledger.intents,
            bankroll=self.bankroll,
            equity=self.account.equity(self.book),
            equity_peak=self.equity_peak,
            max_drawdown=self.max_drawdown,
            max_open=self.max_open,
            learn_exit_orders=self.learn_exit_orders,
            predictions=self.predictions,
            now_ms=int(now.timestamp() * 1000),
        )

    def _stamp_open_entries(self, signal: Signal, stamp: int) -> None:
        for intent in self.account.ledger.intents:
            if intent.status != "open" or intent.entry_quote is not None:
                continue
            if net_open_qty(intent) <= 0:
                continue
            self._stamp_entry(intent, signal, stamp)

    def _stamp_entry(self, intent: PaperIntent, signal: Signal, stamp: int) -> None:
        if intent.entry_quote is not None or intent.opened_ms is None:
            return
        bought, _sold, buy_cost, *_rest = position_legs(intent)
        if bought <= 0:
            return
        avg = buy_cost / bought
        features = intent.entry_features or market_features(
            signal, self.book, intent.outcome, intent.style, avg
        )
        learned = self.learner.wants_exit(features)
        quote = build_quote(
            signal,
            self.book,
            self.params,
            outcome=intent.outcome,
            style=intent.style,
            price=avg,
            avg_price=avg,
            held_ms=0,
            learned=learned,
            learner_p=self.learner.predict(features),
            ts_ms=intent.opened_ms if intent.opened_ms is not None else stamp,
            filled=bought,
        )
        intent.entry_quote = quote
        self._remember_prediction(intent, quote)

    def _record_exit(
        self,
        intent: PaperIntent,
        signal: Signal | None,
        stamp: int,
        reason: str,
        learned_signal: bool,
    ) -> None:
        if intent.exit_delta is not None:
            return
        hold = None if intent.opened_ms is None else stamp - intent.opened_ms
        exit_mark: dict[str, Any] | None = None
        if signal is not None:
            bought, _sold, buy_cost, *_rest = position_legs(intent)
            avg = buy_cost / bought if bought > 0 else intent.price
            features = market_features(signal, self.book, intent.outcome, intent.style, avg)
            exit_mark = build_quote(
                signal,
                self.book,
                self.params,
                outcome=intent.outcome,
                style=intent.style,
                price=avg,
                avg_price=avg,
                held_ms=hold,
                learned=learned_signal,
                learner_p=self.learner.predict(features),
                ts_ms=stamp,
                filled=bought if bought > 0 else Decimal("1"),
            )
            intent.exit_quote = exit_mark
        intent.exit_delta = build_delta(
            intent.entry_quote,
            exit_mark,
            reason=reason,
            hold_ms=hold,
            learn_exit_orders=self.learn_exit_orders,
            learned_signal=learned_signal,
        )

    def _remember_prediction(self, intent: PaperIntent, quote: dict[str, Any]) -> None:
        for row in self.predictions:
            if (
                row.get("ticker") == intent.market_ticker
                and row.get("opened_ms") == intent.opened_ms
                and row.get("outcome") == intent.outcome
            ):
                return
        self.predictions.append(prediction_row(intent, quote))
        self._watch_label(intent.market_ticker)

    def _watch_label(self, ticker: str) -> None:
        for item in self.label_watch:
            if item.ticker == ticker:
                return
        close_at = parse_kxbtc15m_close(ticker)
        now = datetime.now(timezone.utc)
        deadline = (close_at + _OFFICIAL_WAIT) if close_at is not None else (now + _OFFICIAL_WAIT)
        self.label_watch.append(LabelWatch(ticker=ticker, deadline=deadline))

    def _poll_labels(self, now: datetime) -> None:
        if self.lookup is None:
            return
        still: list[LabelWatch] = []
        for item in self.label_watch:
            if item.next_poll is not None and now < item.next_poll:
                still.append(item)
                continue
            item.next_poll = now + _POLL_EVERY
            official = self.lookup.official_result(item.ticker)
            if official == "yes" or official == "no":
                self._apply_settlement_label(item.ticker, official == "yes", "official")
                continue
            if now >= item.deadline:
                continue
            still.append(item)
        self.label_watch = still

    def _apply_settlement_label(self, ticker: str, yes_won: bool, source: str) -> None:
        stamp_settlement(self.predictions, ticker, yes_won=yes_won, source=source)
        if source == "official":
            self.label_watch = [item for item in self.label_watch if item.ticker != ticker]

    def _learn(self, intent: PaperIntent, reason: str) -> None:
        if intent.pnl is None or not intent.entry_features:
            return
        self.learner.update(
            intent.entry_features,
            won=intent.pnl > 0,
            pnl=intent.pnl,
            style=intent.style,
            reason=reason,
        )

    def _strike_for(self, ticker: str) -> Decimal | None:
        if ticker == self.ticker:
            return self.strike
        return self._pending_strike(ticker)

    def _pending_strike(self, ticker: str) -> Decimal | None:
        for item in self.pending:
            if item.ticker == ticker:
                return item.strike
        return None

    def _pending_close(self, ticker: str) -> Decimal | None:
        for item in self.pending:
            if item.ticker == ticker:
                return item.close_avg
        return None


def _arm_for(learn_exit_orders: bool, arm: str | None) -> str:
    if arm is None:
        return "ref" if learn_exit_orders else "no_learn_exit"
    if arm == "legacy":
        if not learn_exit_orders:
            raise ValueError("legacy sessions keep learned-exit orders enabled")
        return arm
    if arm == "ref" and learn_exit_orders:
        return arm
    if arm == "no_learn_exit" and not learn_exit_orders:
        return arm
    raise ValueError(f"arm {arm!r} does not match learn_exit_orders={learn_exit_orders}")


def _arm_label(arm: str) -> str:
    if arm == "ref":
        return "REF"
    if arm == "no_learn_exit":
        return "NO_LEARN_EXIT"
    if arm == "legacy":
        return "LEGACY"
    raise ValueError(f"unknown arm {arm!r}")


def _should_resume(existing: PaperSession, now: datetime) -> bool:
    """Keep a forwardtest that is waiting on min_closes after the clock."""
    if existing.finished_known:
        return not existing.finished
    return existing.ends_at > now


def assert_paper_only(trade_env: str) -> None:
    if trade_env != "paper":
        raise RuntimeError(
            f"paper-btc-15m refuses KALSHI_TRADE_ENV={trade_env!r}. Set KALSHI_TRADE_ENV=paper."
        )


def run_paper_session(
    *,
    bankroll: Decimal,
    hours: float,
    target_return: Decimal,
    jsonl_path: Path,
    session_root: Path = DEFAULT_SESSION_ROOT,
    settings: Settings | None = None,
    learn_exit_orders: bool = True,
    min_closes: int | None = None,
    drawdown_stop: Decimal | None = None,
) -> int:
    """Tail ``jsonl_path`` until the session clock ends. Paper fills stay local."""
    trade_env = settings.trade_env if settings is not None else "paper"
    assert_paper_only(trade_env)
    lock = SessionLock(session_root / "session.lock")
    lock.acquire()
    lookup: KalshiMarketLookup | None = None
    try:
        if (
            settings is not None
            and settings.data_env == "production"
            and settings.has_prod_credentials
        ):
            lookup = KalshiMarketLookup(settings)
            lookup.warn_fee_schedule()
        else:
            log.warning("official results unavailable; reconstructed BRTI is the fallback")
        now = datetime.now(timezone.utc)
        existing = _load_quiet(session_root)
        if existing is not None and _should_resume(existing, now):
            session = existing
            if session.learn_exit_orders != learn_exit_orders:
                raise RuntimeError(
                    f"session {session.session_id} has learn_exit_orders={session.learn_exit_orders}; "
                    f"this process asked for {learn_exit_orders}. Use a new session directory."
                )
            if session.min_closes != min_closes or session.drawdown_stop != drawdown_stop:
                log.warning(
                    "resuming %s keeps min_closes=%s drawdown_stop=%s (CLI was %s / %s)",
                    session.session_id,
                    session.min_closes,
                    session.drawdown_stop,
                    min_closes,
                    drawdown_stop,
                )
            if session.bankroll != bankroll or session.target_return != target_return:
                log.warning(
                    "resuming %s bankroll %s target %s (flags were %s / %s)",
                    session.session_id,
                    format(session.bankroll, "f"),
                    format(session.target_return, "f"),
                    format(bankroll, "f"),
                    format(target_return, "f"),
                )
            log.info("resuming paper session %s", session.session_id)
        else:
            session = PaperSession.create(
                session_root,
                bankroll=bankroll,
                hours=hours,
                target_return=target_return,
                now=now,
                learn_exit_orders=learn_exit_orders,
                min_closes=min_closes,
                drawdown_stop=drawdown_stop,
            )
            log.info("started paper session %s", session.session_id)
        session.lookup = lookup
        _install_signals(session)
        _print_banner(session, jsonl_path, trade_env)
        return session.run_loop(jsonl_path)
    finally:
        if lookup is not None:
            lookup.close()
        lock.release()


def paper_session_dirs(primary: Path = DEFAULT_SESSION_ROOT) -> list[Path]:
    """Legacy session plus the two forwardtest arms, when those directories exist."""
    dirs = [primary]
    for name in (AB_REF_DIR, AB_NOLEARN_DIR):
        child = primary / name
        if child not in dirs:
            dirs.append(child)
    return dirs


def ab_arm_dirs(root: Path) -> tuple[Path, Path]:
    """Isolated roots. Neither path is ``root`` itself, so an old session stays put."""
    return root / AB_REF_DIR, root / AB_NOLEARN_DIR


def read_session_view(root: Path = DEFAULT_SESSION_ROOT) -> dict[str, Any] | None:
    """Dashboard read of the latest saved view. Missing or corrupt files yield None."""
    try:
        if not root.is_dir():
            return None
        pointer_path = root / "current.json"
        if not pointer_path.is_file():
            return None
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        session_id = str(pointer["session_id"])
        payload = json.loads((root / session_id / "state.json").read_text(encoding="utf-8"))
        view = payload.get("view")
        if not isinstance(view, dict):
            return None
        shown = dict(view)
        shown["running"] = session_running(root)
        shown["session_dir"] = str(root)
        return shown
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def session_running(root: Path) -> bool:
    pid = _lock_pid(root / "session.lock")
    return pid is not None and _pid_alive(pid)


def parse_official_result(payload: dict[str, Any]) -> OfficialResult | None:
    """``GET /markets/{ticker}`` result. Empty and scalar are not a yes/no payout."""
    market = payload.get("market")
    if not isinstance(market, dict):
        market = payload
    raw = market.get("result")
    if raw == "yes" or raw == "no":
        return raw
    if raw in (None, "", "scalar"):
        return None
    log.warning("ignoring non-binary market result %r", raw)
    return None


class KalshiMarketLookup:
    """GET-only official result and strike. Production writes stay blocked on the client."""

    def __init__(self, settings: Settings) -> None:
        self._client = KalshiReadClient(settings, timeout=5.0)
        self._results: dict[str, OfficialResult] = {}
        self._strikes: dict[str, Decimal | None] = {}

    def close(self) -> None:
        self._client.close()

    def official_result(self, ticker: str) -> OfficialResult | None:
        cached = self._results.get(ticker)
        if cached is not None:
            return cached
        market = self._market(ticker)
        if market is None:
            return None
        result = parse_official_result({"market": market})
        self._remember_strike(ticker, market)
        if result is not None:
            self._results[ticker] = result
        return result

    def floor_strike(self, ticker: str) -> Decimal | None:
        if ticker in self._strikes:
            return self._strikes[ticker]
        market = self._market(ticker)
        if market is None:
            return None
        return self._remember_strike(ticker, market)

    def warn_fee_schedule(self) -> None:
        """Log if KXBTC15M leaves the quadratic / multiplier-1 schedule this code prices."""
        try:
            payload = self._client.get(f"/series/{SERIES_TICKER_BTC_15M}")
        except Exception as exc:  # noqa: BLE001 — keep the paper loop up
            log.warning("could not refresh KXBTC15M fee schedule: %s", exc)
            return
        series = payload.get("series") if isinstance(payload, dict) else None
        if not isinstance(series, dict):
            return
        fee_type = series.get("fee_type")
        multiplier = series.get("fee_multiplier")
        if fee_type != "quadratic" or Decimal(str(multiplier)) != Decimal("1"):
            log.warning(
                "KXBTC15M fee_type=%s multiplier=%s; paper still uses quadratic taker and zero maker",
                fee_type,
                multiplier,
            )

    def _market(self, ticker: str) -> dict[str, Any] | None:
        try:
            payload = self._client.get_market(ticker)
        except Exception as exc:  # noqa: BLE001 — read failures must not kill the session
            log.warning("official market read failed for %s: %s", ticker, exc)
            return None
        market = payload.get("market") if isinstance(payload, dict) else None
        if isinstance(market, dict):
            return market
        return None

    def _remember_strike(self, ticker: str, market: dict[str, Any]) -> Decimal | None:
        raw = market.get("floor_strike")
        strike = Decimal(str(raw)) if raw is not None else None
        self._strikes[ticker] = strike
        return strike


def _load_quiet(root: Path) -> PaperSession | None:
    try:
        return PaperSession.load(root)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        log.warning("ignoring unreadable paper session under %s", root)
        return None


def _install_signals(session: PaperSession) -> None:
    def _stop(_signum: int, _frame: object) -> None:
        session.request_stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def _print_banner(session: PaperSession, jsonl_path: Path, trade_env: str) -> None:
    print(f"session:       {session.session_id}")
    print(f"arm:           {_arm_label(session.arm)}")
    print(f"learn_exit_orders: {str(session.learn_exit_orders).lower()}")
    print(
        "still active:  training, adjust_params, rolling/mark adapt, "
        "size/cooldown, stop, signal_flip, take_profit, edge_gone, settlement"
    )
    print(f"bankroll:      {format(session.bankroll, 'f')}")
    print(f"target_return: {format(session.target_return, 'f')} (aspirational, not a forecast)")
    print(f"ends:          {_ams(session.ends_at)}")
    if session.min_closes is not None:
        print(f"min_closes:    {session.min_closes} (keeps running past the clock until both are met)")
    if session.drawdown_stop is not None:
        print(f"drawdown_stop: {format(session.drawdown_stop, 'f')} from bankroll")
    print(f"jsonl:         {jsonl_path}")
    print(f"state:         {session.root}")
    print(f"trade_env:     {trade_env}")
    print("orders:        none (local fills only)")


def _adaptation_rows(rows: list[tuple[str, Adaptation]]) -> list[dict[str, Any]]:
    shown = rows[-_VIEW_ADAPTATIONS:]
    out: list[dict[str, Any]] = []
    for at, item in reversed(shown):
        out.append(
            {
                "at": at,
                "at_amsterdam": _ams(_parse_dt(at)),
                "reason": item.reason,
                "params": item.after.to_dict(),
            }
        )
    return out


def _trade_rows(
    ledger: PaperLedger,
    *,
    statuses: tuple[str, ...],
    book: OrderbookState | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for intent in ledger.intents:
        if intent.status not in statuses:
            continue
        bought, sold, buy_cost, buy_fees, _proceeds, sell_fees = position_legs(intent)
        net = bought - sold
        if net < 0:
            net = Decimal("0")
        shown = net if intent.status == "open" else bought
        fee = buy_fees + sell_fees
        first_ts = intent.opened_ms
        if first_ts is None:
            first_ts = next((fill.ts_ms for fill in intent.fills if fill.ts_ms is not None), None)
        last_ts = next((fill.ts_ms for fill in reversed(intent.fills) if fill.ts_ms is not None), None)
        hold_ms = None
        if intent.exit_delta and intent.exit_delta.get("hold_ms") is not None:
            hold_ms = int(intent.exit_delta["hold_ms"])
        elif first_ts is not None and last_ts is not None and last_ts >= first_ts:
            hold_ms = last_ts - first_ts
        hold_s = None if hold_ms is None else hold_ms / 1000.0
        mark = _position_mark(book, intent)
        unrealized = None
        if intent.status == "open" and mark is not None and bought > 0 and net > 0:
            unrealized = mark * net - (buy_cost * net / bought) - (buy_fees * net / bought)
        rows.append(
            {
                "ticker": intent.market_ticker,
                "outcome": intent.outcome,
                "style": intent.style,
                "limit": format(intent.price, "f"),
                "count": format(intent.count, "f"),
                "remaining": format(intent.remaining, "f"),
                "filled": format(shown, "f"),
                "sold": format(sold, "f"),
                "avg_price": format(buy_cost / bought, "f") if bought > 0 else None,
                "fee": format(fee, "f"),
                "status": intent.status,
                "won": intent.won,
                "pnl": _num(intent.pnl),
                "exit_reason": intent.exit_reason,
                "hold_ms": hold_ms,
                "exit_delta": intent.exit_delta,
                "mark": _num(mark) if intent.status == "open" else None,
                "unrealized": _num(unrealized),
                "hold_s": hold_s,
                "at_amsterdam": _ams_ms(first_ts),
            }
        )
    rows.reverse()
    return rows


def _fill_rows(ledger: PaperLedger) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for intent in ledger.intents:
        for fill in intent.fills:
            rows.append(
                {
                    "ticker": fill.market_ticker,
                    "outcome": fill.outcome,
                    "style": fill.style,
                    "price": format(fill.price, "f"),
                    "count": format(fill.count, "f"),
                    "fee": format(fill.fee, "f"),
                    "source": fill.source,
                    "action": fill.action,
                    "at_amsterdam": _ams_ms(fill.ts_ms),
                }
            )
    rows.reverse()
    return rows


def _closed_pnls(ledger: PaperLedger) -> list[Decimal]:
    return [
        intent.pnl
        for intent in ledger.intents
        if intent.status in ("settled", "closed") and intent.fills and intent.pnl is not None
    ]


def _ticker_busy(ledger: PaperLedger, ticker: str) -> bool:
    for intent in ledger.intents:
        if intent.market_ticker != ticker or intent.status != "open":
            continue
        if intent.remaining > 0 or net_open_qty(intent) > 0:
            return True
    return False


def _open_guard_key(ledger: PaperLedger) -> str | None:
    for intent in ledger.intents:
        if intent.status == "open" and net_open_qty(intent) > 0:
            return f"{intent.market_ticker}:{intent.opened_ms}"
    return None


def _position_mark(book: OrderbookState | None, intent: PaperIntent) -> Decimal | None:
    if book is None or not book.valid or book.market_ticker != intent.market_ticker:
        return None
    return book.best_bid(intent.outcome)


def _has_open(ledger: PaperLedger, ticker: str) -> bool:
    return any(intent.market_ticker == ticker and intent.status == "open" for intent in ledger.intents)


def _intent_dict(intent: PaperIntent) -> dict[str, Any]:
    return {
        "market_ticker": intent.market_ticker,
        "outcome": intent.outcome,
        "price": format(intent.price, "f"),
        "count": format(intent.count, "f"),
        "style": intent.style,
        "remaining": format(intent.remaining, "f"),
        "status": intent.status,
        "settled": intent.settled,
        "won": intent.won,
        "pnl": _num(intent.pnl),
        "exit_reason": intent.exit_reason,
        "entry_features": intent.entry_features,
        "entry_quote": intent.entry_quote,
        "exit_quote": intent.exit_quote,
        "exit_delta": intent.exit_delta,
        "opened_ms": intent.opened_ms,
        "fills": [
            {
                "market_ticker": fill.market_ticker,
                "style": fill.style,
                "outcome": fill.outcome,
                "price": format(fill.price, "f"),
                "count": format(fill.count, "f"),
                "fee": format(fill.fee, "f"),
                "ts_ms": fill.ts_ms,
                "source": fill.source,
                "action": fill.action,
            }
            for fill in intent.fills
        ],
    }


def _intent_from(raw: dict[str, Any]) -> PaperIntent:
    intent = PaperIntent(
        market_ticker=str(raw["market_ticker"]),
        outcome=raw["outcome"],
        price=Decimal(str(raw["price"])),
        count=Decimal(str(raw["count"])),
        style=raw["style"],
    )
    intent.remaining = Decimal(str(raw["remaining"]))
    intent.status = raw["status"]
    intent.settled = bool(raw.get("settled"))
    intent.won = raw.get("won")
    intent.pnl = _dec(raw.get("pnl"))
    intent.exit_reason = raw.get("exit_reason")
    intent.entry_features = [float(value) for value in raw.get("entry_features") or []]
    intent.entry_quote = raw.get("entry_quote") if isinstance(raw.get("entry_quote"), dict) else None
    intent.exit_quote = raw.get("exit_quote") if isinstance(raw.get("exit_quote"), dict) else None
    intent.exit_delta = raw.get("exit_delta") if isinstance(raw.get("exit_delta"), dict) else None
    intent.opened_ms = raw.get("opened_ms")
    intent.fills = [
        PaperFill(
            market_ticker=str(fill["market_ticker"]),
            style=fill["style"],
            outcome=fill["outcome"],
            price=Decimal(str(fill["price"])),
            count=Decimal(str(fill["count"])),
            fee=Decimal(str(fill["fee"])),
            ts_ms=fill.get("ts_ms"),
            source=str(fill.get("source") or ""),
            action=fill.get("action") or "buy",
        )
        for fill in raw.get("fills") or []
    ]
    return intent


def _brti_spot(msg: dict[str, Any]) -> Decimal | None:
    raw = msg.get("data")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    value = parsed.get("value")
    if value is None:
        return None
    return Decimal(str(value))


def _seconds_left(ticker: str, now: datetime) -> float | None:
    close_at = parse_kxbtc15m_close(ticker)
    if close_at is None:
        return None
    return (close_at - now).total_seconds()


def _ams(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_AMS_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _ams_ms(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return _ams(datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc))


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _num(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _line_at(path: Path, offset: int) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        return json.loads(fh.readline())


def _last_offset_before(path: Path, needles: tuple[bytes, ...], before: int) -> int | None:
    end = min(before, path.stat().st_size)
    found: int | None = None
    while end > 0:
        start = max(0, end - _CHUNK_BYTES)
        with path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(end - start)
        if start > 0:
            newline = raw.find(b"\n")
            if newline < 0:
                end = start
                continue
            raw = raw[newline + 1 :]
            cursor = start + newline + 1
        else:
            cursor = 0
        pos = cursor
        for line in raw.split(b"\n"):
            if any(needle in line for needle in needles):
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
