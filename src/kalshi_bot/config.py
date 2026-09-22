"""Environment configuration — demo plumbing, production data, paper trades."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Official hosts: https://docs.kalshi.com/getting_started/api_environments
DEMO_REST_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
PROD_REST_BASE = "https://external-api.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"
SERIES_TICKER_BTC_15M = "KXBTC15M"
BRTI_INDEX_ID = "BRTI"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROD_KEY = _REPO_ROOT / "keys" / "prod-kalshi.key"
_DEFAULT_PROD_ID_FILE = _REPO_ROOT / "keys" / "prod-key-id.txt"

_TRADE_ENVS = frozenset({"paper", "demo"})
_DATA_ENVS = frozenset({"production", "demo"})
_ALLOWED_LEGACY_ENVS = frozenset({"demo"})


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from the environment."""

    env: str
    trade_env: str
    data_env: str
    rest_base: str
    ws_url: str
    prod_rest_base: str
    prod_ws_url: str
    api_key_id: str | None
    private_key_path: Path | None
    prod_api_key_id: str | None
    prod_private_key_path: Path | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path and self.private_key_path.is_file())

    @property
    def has_prod_credentials(self) -> bool:
        return bool(
            self.prod_api_key_id
            and self.prod_private_key_path
            and self.prod_private_key_path.is_file()
        )


def _optional_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    return Path(raw).expanduser()


def _read_id_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        value = line.strip()
        if value and value != "PASTE_PROD_KEY_ID_HERE":
            return value
    return None


def load_settings(*, dotenv_path: str | Path | None = None) -> Settings:
    """Load settings from `.env` / process env.

    `KALSHI_ENV` stays demo-only so the existing demo client cannot point at
    production. Live analysis uses `KALSHI_DATA_ENV=production`.
    `KALSHI_TRADE_ENV=live` is rejected.
    """
    load_dotenv(dotenv_path)

    env = (os.getenv("KALSHI_ENV") or "demo").strip().lower()
    if env not in _ALLOWED_LEGACY_ENVS:
        raise ValueError(
            f"KALSHI_ENV={env!r} is not allowed. Demo plumbing stays on demo "
            f"(set KALSHI_ENV=demo). Use KALSHI_DATA_ENV=production for live data."
        )

    trade_env = (os.getenv("KALSHI_TRADE_ENV") or "paper").strip().lower()
    if trade_env == "live":
        raise ValueError("KALSHI_TRADE_ENV=live is refused. Use paper or demo.")
    if trade_env not in _TRADE_ENVS:
        raise ValueError(f"KALSHI_TRADE_ENV={trade_env!r} is not allowed. Use paper or demo.")

    data_env = (os.getenv("KALSHI_DATA_ENV") or "production").strip().lower()
    if data_env not in _DATA_ENVS:
        raise ValueError(
            f"KALSHI_DATA_ENV={data_env!r} is not allowed. Use production or demo."
        )

    api_key_id = (os.getenv("KALSHI_API_KEY_ID") or "").strip() or None
    private_key_path = _optional_path((os.getenv("KALSHI_PRIVATE_KEY_PATH") or "").strip() or None)

    prod_api_key_id = (os.getenv("KALSHI_PROD_API_KEY_ID") or "").strip() or None
    if prod_api_key_id is None:
        prod_api_key_id = _read_id_file(_DEFAULT_PROD_ID_FILE)

    prod_key_raw = (os.getenv("KALSHI_PROD_PRIVATE_KEY_PATH") or "").strip() or None
    prod_private_key_path = _optional_path(prod_key_raw)
    if prod_private_key_path is None and _DEFAULT_PROD_KEY.is_file():
        prod_private_key_path = _DEFAULT_PROD_KEY

    return Settings(
        env=env,
        trade_env=trade_env,
        data_env=data_env,
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        prod_rest_base=PROD_REST_BASE,
        prod_ws_url=PROD_WS_URL,
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        prod_api_key_id=prod_api_key_id,
        prod_private_key_path=prod_private_key_path,
    )


def require_prod_credentials(settings: Settings) -> Settings:
    """Fail hard when the production data stream cannot be authenticated."""
    if settings.data_env != "production":
        raise ValueError(
            f"KALSHI_DATA_ENV={settings.data_env!r} cannot drive live analysis. "
            "Set KALSHI_DATA_ENV=production."
        )
    if not settings.has_prod_credentials:
        raise RuntimeError(
            "Production credentials are required for live data. Set "
            "KALSHI_PROD_API_KEY_ID and KALSHI_PROD_PRIVATE_KEY_PATH "
            "(or keys/prod-key-id.txt + keys/prod-kalshi.key). "
            "There is no REST-orderbook fallback."
        )
    return settings
