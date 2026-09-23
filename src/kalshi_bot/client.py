"""Thin Trade API HTTP clients (public + RSA-PSS authenticated)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from kalshi_bot.auth import auth_headers, load_private_key
from kalshi_bot.config import DEMO_REST_BASE, PROD_REST_BASE, Settings

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def locked_relative_path(base: str, path: str) -> str:
    """Reject absolute URLs so httpx cannot drop the locked base host."""
    raw = path.strip()
    lowered = raw.lower()
    if (
        not raw
        or "\\" in raw
        or raw.startswith("//")
        or "://" in lowered
    ):
        raise ValueError(f"refusing absolute URL: {path!r}")
    relative = raw.lstrip("/")
    if not relative or "://" in relative.lower() or relative.startswith("//"):
        raise ValueError(f"refusing absolute URL: {path!r}")
    base_url = base.rstrip("/") + "/"
    resolved = urljoin(base_url, relative)
    base_parts = urlsplit(base_url)
    parts = urlsplit(resolved)
    if (
        parts.scheme.lower() != (base_parts.scheme or "").lower()
        or (parts.hostname or "").lower() != (base_parts.hostname or "").lower()
        or parts.port != base_parts.port
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError(f"refusing absolute URL outside locked host: {resolved}")
    return relative


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
        relative = locked_relative_path(self.settings.rest_base, path)
        return urljoin(self.settings.rest_base.rstrip("/") + "/", relative)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if method.upper() in _WRITE_METHODS and json_body is None:
            raise ValueError("Demo writes require an explicit json_body.")
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

        response = self._client.request(
            method,
            locked_relative_path(self.settings.rest_base, path),
            params=params,
            headers=headers,
            json=json_body,
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def post(self, path: str, *, json_body: dict[str, Any], authenticated: bool = True) -> Any:
        """Demo-only write helper. Production hosts never reach this client."""
        return self.request("POST", path, authenticated=authenticated, json_body=json_body)

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


class KalshiReadClient:
    """Production GET-only client. POST/PUT/PATCH/DELETE are refused."""

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        if settings.prod_rest_base != PROD_REST_BASE:
            raise ValueError(
                f"Refusing non-production REST base: {settings.prod_rest_base!r}. "
                f"Expected {PROD_REST_BASE!r}."
            )
        if not settings.has_prod_credentials:
            raise RuntimeError(
                "KalshiReadClient requires production credentials. "
                "There is no unauthenticated fallback for the live stream."
            )
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.prod_rest_base.rstrip("/") + "/",
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        assert settings.prod_private_key_path is not None
        self._private_key = load_private_key(settings.prod_private_key_path)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KalshiReadClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _absolute_url(self, path: str) -> str:
        relative = locked_relative_path(self.settings.prod_rest_base, path)
        return urljoin(self.settings.prod_rest_base.rstrip("/") + "/", relative)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        if method.upper() in _WRITE_METHODS:
            raise PermissionError(
                f"Production writes are blocked ({method.upper()} {path}). "
                "Paper execution stays local."
            )
        headers: dict[str, str] = {}
        if authenticated:
            assert self.settings.prod_api_key_id is not None
            headers.update(
                auth_headers(
                    api_key_id=self.settings.prod_api_key_id,
                    private_key=self._private_key,
                    method=method,
                    url_or_path=self._absolute_url(path),
                )
            )
        response = self._client.request(
            method,
            locked_relative_path(self.settings.prod_rest_base, path),
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def get(self, path: str, *, params: dict[str, Any] | None = None, authenticated: bool = True) -> Any:
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

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self.get(f"/markets/{ticker}")
