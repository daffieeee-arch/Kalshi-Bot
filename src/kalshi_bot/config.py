"""Environment configuration — DEMO only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Official demo Trade API endpoints (docs.kalshi.com/getting_started/api_environments)
DEMO_REST_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
SERIES_TICKER_BTC_15M = "KXBTC15M"

_ALLOWED_ENVS = frozenset({"demo"})


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from the environment."""

    env: str
    rest_base: str
    ws_url: str
    api_key_id: str | None
    private_key_path: Path | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path and self.private_key_path.is_file())


def load_settings(*, dotenv_path: str | Path | None = None) -> Settings:
    """Load settings from `.env` / process env. Rejects anything other than demo."""
    load_dotenv(dotenv_path)

    env = (os.getenv("KALSHI_ENV") or "demo").strip().lower()
    if env not in _ALLOWED_ENVS:
        raise ValueError(
            f"KALSHI_ENV={env!r} is not allowed. This project is DEMO-only "
            f"(set KALSHI_ENV=demo). Production endpoints are intentionally blocked."
        )

    api_key_id = (os.getenv("KALSHI_API_KEY_ID") or "").strip() or None
    key_path_raw = (os.getenv("KALSHI_PRIVATE_KEY_PATH") or "").strip() or None
    private_key_path = Path(key_path_raw).expanduser() if key_path_raw else None

    return Settings(
        env=env,
        rest_base=DEMO_REST_BASE,
        ws_url=DEMO_WS_URL,
        api_key_id=api_key_id,
        private_key_path=private_key_path,
    )
