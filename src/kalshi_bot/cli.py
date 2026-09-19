"""CLI entrypoints for the Kalshi DEMO scaffold."""

from __future__ import annotations

import argparse
import json
import sys

from kalshi_bot.client import KalshiDemoClient
from kalshi_bot.config import SERIES_TICKER_BTC_15M, load_settings
from kalshi_bot.discover import discover_btc_15m, format_market_line


CREDENTIALS_HELP = """
No demo credentials configured (or private key file missing).

Public market discovery still works. To enable authenticated calls (e.g. balance):

  1. Open https://demo.kalshi.co/ and create / log into a demo account
  2. Account settings → API Keys → Create New API Key
  3. Save the Key ID and the downloaded private key (.key) outside git
  4. Copy .env.example → .env and set:
       KALSHI_ENV=demo
       KALSHI_API_KEY_ID=<key-id>
       KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/demo-key.key

Never commit .env or *.key / *.pem files.
""".strip()


def cmd_discover_btc_15m(args: argparse.Namespace) -> int:
    settings = load_settings()
    with KalshiDemoClient(settings) as client:
        series = client.get_series(SERIES_TICKER_BTC_15M)
        series_info = series.get("series") or {}
        markets = discover_btc_15m(client)

        print(f"env:          {settings.env}")
        print(f"rest:         {settings.rest_base}")
        print(f"ws:           {settings.ws_url}")
        print(f"series:       {SERIES_TICKER_BTC_15M} — {series_info.get('title')}")
        print(f"credentials:  {'present' if settings.has_credentials else 'missing (public-only)'}")
        print(f"open markets: {len(markets)}")
        print()

        if args.json:
            print(json.dumps(markets, indent=2))
        else:
            if not markets:
                print("No currently open KXBTC15M markets on demo (try again next window).")
            for summary in markets:
                print(format_market_line(summary))
                print()

        if settings.has_credentials:
            try:
                balance = client.get_balance()
                cents = balance.get("balance")
                dollars = (cents / 100) if isinstance(cents, (int, float)) else cents
                print(f"demo balance: ${dollars} (raw={balance})")
            except Exception as exc:  # noqa: BLE001 — CLI surface
                print(f"balance call failed: {exc}", file=sys.stderr)
                return 1
        else:
            print(CREDENTIALS_HELP)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discover-btc-15m",
        description="Discover open Kalshi DEMO Bitcoin 15-minute (KXBTC15M) markets.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print market summaries as JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(cmd_discover_btc_15m(args))


if __name__ == "__main__":
    main()
