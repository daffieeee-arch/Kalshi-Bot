"""WebSocket signing, backoff, and trade parsing. No live socket."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.auth import load_private_key
from kalshi_bot.config import DEMO_REST_BASE, DEMO_WS_URL, Settings
from kalshi_bot.ws import WS_SIGN_PATH, backoff_delay, parse_trade, ws_auth_headers

FIXTURES = Path(__file__).parent / "fixtures"


def _settings(path: Path) -> Settings:
    return Settings(
        env="demo",
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        api_key_id="test-key-id",
        private_key_path=path,
    )


def _write_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def test_ws_signature_is_get_ws_path(tmp_path: Path) -> None:
    key_path = tmp_path / "demo.key"
    _write_key(key_path)
    headers = ws_auth_headers(_settings(key_path), timestamp_ms=1700000000000)
    message = f"1700000000000GET{WS_SIGN_PATH}".encode()
    load_private_key(key_path).public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert WS_SIGN_PATH == "/trade-api/ws/v2"


def test_backoff_is_capped_and_jittered() -> None:
    assert backoff_delay(1, lambda: 0.0) == 0.5
    assert backoff_delay(1, lambda: 1.0) == 1.0
    assert backoff_delay(10, lambda: 1.0) == 60.0


def test_trade_parser_does_not_invent_aggressor() -> None:
    envelope = json.loads((FIXTURES / "trade_without_aggressor.json").read_text())
    parsed = parse_trade(envelope)
    assert parsed["trade_id"] == "d91bc706-ee49-470d-82d8-11418bda6fed"
    assert parsed["source_ts"] == 1669149841000
    assert "taker_side" not in parsed
    assert "taker_outcome_side" not in parsed
    assert "taker_book_side" not in parsed


def test_trade_parser_keeps_aggressor_when_sent() -> None:
    envelope = json.loads((FIXTURES / "trade_without_aggressor.json").read_text())
    envelope["msg"]["taker_side"] = "no"
    envelope["msg"]["taker_outcome_side"] = "yes"
    envelope["msg"]["taker_book_side"] = "ask"
    parsed = parse_trade(envelope)
    assert parsed["taker_side"] == "no"
    assert parsed["taker_outcome_side"] == "yes"
    assert parsed["taker_book_side"] == "ask"
