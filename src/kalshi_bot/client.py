"""Thin DEMO Trade API HTTP client (public + RSA-PSS authenticated)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from kalshi_bot.auth import auth_headers, load_private_key
from kalshi_bot.config import DEMO_REST_BASE, Settings


class KalshiDemoClient:
    """HTTP client locked to the Kalshi DEMO Trade API host."""

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        if settings.rest_base != DEMO_REST_BASE:
            raise ValueError(
                f"Refusing non-demo REST base: {settings.rest_base!r}. "
                f"Expected {DEMO_REST_BASE!r}."
            )
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.rest_base.rstrip("/") + "/",
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._private_key = None
        if settings.has_credentials:
            assert settings.private_key_path is not None
            self._private_key = load_private_key(settings.private_key_path)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KalshiDemoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _absolute_url(self, path: str) -> str:
        return urljoin(self.settings.rest_base.rstrip("/") + "/", path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> Any:
        headers: dict[str, str] = {}
        if authenticated:
            if not self.settings.has_credentials or self._private_key is None:
                raise RuntimeError(
                    "Authenticated request requires KALSHI_API_KEY_ID and "
                    "KALSHI_PRIVATE_KEY_PATH pointing at a readable PEM key. "
                    "Create demo keys at https://demo.kalshi.co/ (Account → API Keys)."
                )
            assert self.settings.api_key_id is not None
            # Sign the full path from API root (no query), per Kalshi docs.
            sign_url = self._absolute_url(path)
            headers.update(
                auth_headers(
                    api_key_id=self.settings.api_key_id,
                    private_key=self._private_key,
                    method=method,
                    url_or_path=sign_url,
                )
            )

        response = self._client.request(method, path.lstrip("/"), params=params, headers=headers)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def get(self, path: str, *, params: dict[str, Any] | None = None, authenticated: bool = False) -> Any:
        return self.request("GET", path, params=params, authenticated=authenticated)

    def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = "open",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self.get("/markets", params=params)

    def get_events(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = "open",
        with_nested_markets: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self.get("/events", params=params)

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        return self.get(f"/series/{series_ticker}")

    def get_balance(self) -> dict[str, Any]:
        """Authenticated portfolio balance (demo funds)."""
        return self.get("/portfolio/balance", authenticated=True)
