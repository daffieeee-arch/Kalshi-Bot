"""Live smoke tests against the Kalshi DEMO public Trade API."""

from __future__ import annotations

import pytest

from kalshi_bot.auth import auth_headers, load_private_key, sign_pss_text, signing_path
from kalshi_bot.client import KalshiDemoClient
from kalshi_bot.config import DEMO_REST_BASE, DEMO_WS_URL, SERIES_TICKER_BTC_15M, load_settings
from kalshi_bot.discover import discover_btc_15m, is_currently_open, summarize_market

pytestmark = pytest.mark.integration


def test_settings_are_demo_only() -> None:
    settings = load_settings()
    assert settings.env == "demo"
    assert settings.rest_base == DEMO_REST_BASE
    assert settings.ws_url == DEMO_WS_URL
    assert "demo" in settings.rest_base
    assert "kalshi.com" not in settings.rest_base  # production host blocked by design


def test_signing_path_strips_query() -> None:
    assert (
        signing_path("https://external-api.demo.kalshi.co/trade-api/v2/portfolio/orders?limit=5")
        == "/trade-api/v2/portfolio/orders"
    )


def test_discover_open_kxbtc15m_on_demo() -> None:
    settings = load_settings()
    with KalshiDemoClient(settings) as client:
        series = client.get_series(SERIES_TICKER_BTC_15M)
        assert series["series"]["ticker"] == SERIES_TICKER_BTC_15M

        raw = client.get_markets(series_ticker=SERIES_TICKER_BTC_15M, status="open", limit=20)
        assert "markets" in raw
        assert isinstance(raw["markets"], list)

        # At least one market row should come back for the series filter (demo may
        # include stale rows; we still prove the public endpoint works).
        assert len(raw["markets"]) >= 1
        sample = raw["markets"][0]
        assert sample["ticker"].startswith("KXBTC15M")
        summary = summarize_market(sample)
        for key in ("ticker", "title", "close_time", "yes_bid", "yes_ask", "floor_strike"):
            assert key in summary

        open_summaries = discover_btc_15m(client)
        # Prefer currently open windows when present; allow empty between windows.
        for s in open_summaries:
            assert s["ticker"].startswith("KXBTC15M")
            assert s["title"]
            assert s["close_time"]


def test_is_currently_open_filters_closed_status() -> None:
    assert not is_currently_open(
        {
            "status": "closed",
            "close_time": "2099-01-01T00:00:00Z",
        }
    )


def test_auth_headers_shape_with_ephemeral_key(tmp_path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "demo.key"
    path.write_bytes(pem)

    loaded = load_private_key(path)
    sig = sign_pss_text(loaded, "1700000000000GET/trade-api/v2/portfolio/balance")
    assert isinstance(sig, str) and len(sig) > 20

    headers = auth_headers(
        api_key_id="test-key-id",
        private_key=loaded,
        method="GET",
        url_or_path="https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance",
        timestamp_ms=1700000000000,
    )
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert headers["KALSHI-ACCESS-SIGNATURE"]
