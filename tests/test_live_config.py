"""DATA/TRADE env split and production write lock."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.client import KalshiReadClient
from kalshi_bot.config import (
    DEMO_REST_BASE,
    PROD_REST_BASE,
    PROD_WS_URL,
    load_settings,
    require_prod_credentials,
)
from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.record import extract_ts_ms
from kalshi_bot.ws import ProductionWebSocket


def test_settings_keep_demo_rest_for_plumbing() -> None:
    settings = load_settings()
    assert settings.env == "demo"
    assert settings.rest_base == DEMO_REST_BASE
    assert settings.prod_rest_base == PROD_REST_BASE
    assert settings.prod_ws_url == PROD_WS_URL
    assert "kalshi.com" not in settings.rest_base


def test_trade_env_live_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_TRADE_ENV", "live")
    with pytest.raises(ValueError, match="refused"):
        load_settings()


def test_data_env_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_DATA_ENV", "staging")
    with pytest.raises(ValueError, match="KALSHI_DATA_ENV"):
        load_settings()


def test_require_prod_credentials_without_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KALSHI_DATA_ENV", "production")
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", "")
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY_PATH", str(tmp_path / "missing.key"))
    monkeypatch.setattr("kalshi_bot.config._DEFAULT_PROD_KEY", tmp_path / "nope.key")
    monkeypatch.setattr("kalshi_bot.config._DEFAULT_PROD_ID_FILE", tmp_path / "nope-id.txt")
    settings = load_settings()
    with pytest.raises(RuntimeError, match="Production credentials"):
        require_prod_credentials(settings)


def test_read_client_refuses_writes(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "prod.key"
    path.write_bytes(pem)
    settings = replace(
        load_settings(),
        prod_api_key_id="00000000-0000-0000-0000-000000000000",
        prod_private_key_path=path,
    )
    client = KalshiReadClient(settings)
    with pytest.raises(PermissionError, match="Production writes"):
        client.request("POST", "/portfolio/events/orders")
    client.close()


def test_ws_refuses_non_production_url() -> None:
    settings = replace(
        load_settings(),
        prod_ws_url="wss://example.test/ws",
        prod_api_key_id="00000000-0000-0000-0000-000000000000",
        prod_private_key_path=Path("/tmp/x"),
    )
    with pytest.raises(ValueError, match="non-production WS"):
        ProductionWebSocket(settings)


def test_quadratic_taker_fee_ceils_six_dp() -> None:
    fee = quadratic_taker_fee(Decimal("10"), Decimal("0.50"))
    # 0.07 * 10 * 0.5 * 0.5 = 0.175
    assert fee == Decimal("0.175000")


def test_extract_ts_ms_prefers_payload() -> None:
    assert extract_ts_ms({"msg": {"ts_ms": 9, "received_at": 8}}) == 9
    assert extract_ts_ms({"msg": {"received_at": 8}}) == 8
    assert extract_ts_ms({"type": "ok"}) is None
