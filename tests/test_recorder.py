"""Recorder settlement follows the closed window, not the rolled ticker."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import load_settings
from kalshi_bot.discover import parse_kxbtc15m_close
from kalshi_bot.paper import PaperIntent, PaperLedger
from kalshi_bot.record import JsonlWriter
from kalshi_bot.recorder import DISCOVER_INTERVAL_S, SETTLE_PENDING_S, Recorder


def _settings(tmp_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "prod.key"
    path.write_bytes(pem)
    return replace(
        load_settings(),
        data_env="production",
        prod_api_key_id="00000000-0000-0000-0000-000000000000",
        prod_private_key_path=path,
    )


def _recorder(tmp_path: Path) -> Recorder:
    return Recorder(
        _settings(tmp_path),
        jsonl_path=tmp_path / "out.jsonl",
        lock_path=tmp_path / "recorder.lock",
        paper=PaperLedger(),
    )


def _intent(ticker: str) -> PaperIntent:
    return PaperIntent(
        market_ticker=ticker,
        outcome="yes",
        price=Decimal("0.50"),
        count=Decimal("1"),
        style="maker",
    )


def _close_avg(value: str, window_size: int = 60) -> dict:
    return {
        "last_60s_windowed_average_15min": {
            "value": value,
            "window_size": window_size,
            "window_start_ts_ms": 1,
            "window_end_ts_exclusive": 2,
        }
    }


def test_settle_uses_closed_window_after_roll(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.paper.add(_intent("KXBTC15M-OLD"))
    rec.ticker = "KXBTC15M-OLD"
    rec.floor_strike = Decimal("100")
    rec._remember_closed_window(now=1000.0)
    rec.ticker = "KXBTC15M-NEW"
    rec.floor_strike = Decimal("200")
    rec._maybe_settle(_close_avg("150"), now=1010.0)
    intent = rec.paper.intents[0]
    assert intent.settled
    assert intent.won is True
    assert rec.pending_settle_ticker is None


def test_expired_pending_does_not_steal_next_close(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.paper.add(_intent("KXBTC15M-NEW"))
    rec.ticker = "KXBTC15M-OLD"
    rec.floor_strike = Decimal("100")
    rec._remember_closed_window(now=1000.0)
    rec.ticker = "KXBTC15M-NEW"
    rec.floor_strike = Decimal("200")
    rec._maybe_settle(_close_avg("250"), now=1000.0 + SETTLE_PENDING_S + 1)
    intent = rec.paper.intents[0]
    assert intent.settled
    assert intent.won is True


def test_incomplete_window_does_not_settle(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.paper.add(_intent("KXBTC15M-OLD"))
    rec.ticker = "KXBTC15M-OLD"
    rec.floor_strike = Decimal("100")
    rec._maybe_settle(_close_avg("150", window_size=14))
    assert rec.paper.intents[0].settled is False


def test_parseable_ticker_rejects_foreign_close_window(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    ticker = "KXBTC15M-26SEP221015-15"
    rec.paper.add(_intent(ticker))
    rec.ticker = ticker
    rec.floor_strike = Decimal("100")
    rec._maybe_settle(_close_avg("150"))
    assert rec.paper.intents[0].settled is False
    close = parse_kxbtc15m_close(ticker)
    assert close is not None
    close_ms = int(close.timestamp() * 1000)
    rec._maybe_settle(
        {
            "last_60s_windowed_average_15min": {
                "value": "150",
                "window_size": 60,
                "window_start_ts_ms": close_ms - 60_000,
                "window_end_ts_exclusive": close_ms,
            }
        }
    )
    assert rec.paper.intents[0].settled is True
    assert rec.paper.intents[0].won is True


def test_write_market_meta_records_strike(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.ticker = "KXBTC15M-TEST"
    rec.floor_strike = Decimal("86000")
    writer = JsonlWriter(tmp_path / "meta.jsonl")
    try:
        rec._write_market_meta(writer)
    finally:
        writer.close()
    row = json.loads((tmp_path / "meta.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["stream"] == "market_meta"
    assert row["payload"]["ticker"] == "KXBTC15M-TEST"
    assert row["payload"]["floor_strike"] == "86000"


def test_discover_throttled_when_ticker_missing(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.ticker = None
    rec._last_discover = 0.0

    class _Rest:
        def __init__(self) -> None:
            self.calls = 0

        def get_markets(self, **_kwargs):
            self.calls += 1
            return {"markets": []}

    rest = _Rest()

    async def _run() -> None:
        await rec._maybe_discover(object(), rest)  # type: ignore[arg-type]
        first = rest.calls
        await rec._maybe_discover(object(), rest)  # type: ignore[arg-type]
        assert first == 1
        assert rest.calls == 1
        assert DISCOVER_INTERVAL_S == 15.0

    asyncio.run(_run())
