"""Demo execution lock: production hosts and order methods stay unreachable."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.auth import load_private_key
from kalshi_bot.client import KalshiDemoClient
from kalshi_bot.config import DEMO_REST_BASE, DEMO_WS_URL, Settings, load_settings
from kalshi_bot.ws import ws_auth_headers


def test_production_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_ENV", "production")
    with pytest.raises(ValueError, match="DEMO-only"):
        load_settings()


def test_client_refuses_production_rest_base() -> None:
    settings = Settings(
        env="demo",
        rest_base="https://external-api.kalshi.com/trade-api/v2",
        ws_url=DEMO_WS_URL,
        api_key_id=None,
        private_key_path=None,
    )
    with pytest.raises(ValueError, match="non-demo"):
        KalshiDemoClient(settings)


def test_ws_refuses_production_url(tmp_path: Path) -> None:
    settings = Settings(
        env="demo",
        rest_base=DEMO_REST_BASE,
        ws_url="wss://external-api-ws.kalshi.com/trade-api/ws/v2",
        api_key_id="test-key-id",
        private_key_path=tmp_path / "missing.key",
    )
    with pytest.raises(ValueError, match="non-demo"):
        ws_auth_headers(settings)


def test_client_has_no_order_api_and_no_production_host() -> None:
    assert not hasattr(KalshiDemoClient, "create_order")
    package = Path(__file__).resolve().parents[1] / "src" / "kalshi_bot"
    source = "\n".join(path.read_text() for path in package.glob("*.py"))
    assert "external-api.kalshi.com" not in source
    assert "api.elections.kalshi.com" not in source
    assert "create_order" not in source


def test_post_sends_json_and_signs_path(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "demo.key"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    settings = Settings(
        env="demo",
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        api_key_id="test-key-id",
        private_key_path=key_path,
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        captured["sig"] = request.headers["KALSHI-ACCESS-SIGNATURE"]
        captured["ts"] = request.headers["KALSHI-ACCESS-TIMESTAMP"]
        return httpx.Response(200, json={"ok": True})

    client = KalshiDemoClient(settings)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=DEMO_REST_BASE + "/")
    try:
        body = client.post(
            "/portfolio/orders",
            json_body={"ticker": "KXBTC15M-TEST"},
            authenticated=True,
        )
    finally:
        client.close()

    assert body == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["path"] == "/trade-api/v2/portfolio/orders"
    assert captured["body"] == {"ticker": "KXBTC15M-TEST"}
    message = f"{captured['ts']}POST/trade-api/v2/portfolio/orders".encode()
    load_private_key(key_path).public_key().verify(
        base64.b64decode(str(captured["sig"])),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
