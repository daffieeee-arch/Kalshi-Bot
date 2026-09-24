"""CLI entrypoints for discover (demo) and the production recorder."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import uvicorn

from kalshi_bot.client import KalshiDemoClient, KalshiReadClient
from kalshi_bot.config import SERIES_TICKER_BTC_15M, load_settings, require_prod_credentials
from kalshi_bot.dashboard.app import create_app
from kalshi_bot.discover import discover_btc_15m, format_market_line
from kalshi_bot.paper import PaperIntent
from kalshi_bot.recorder import DEFAULT_JSONL, run_recorder
from kalshi_bot.replay import collect_tickers, fetch_markets, replay
from kalshi_bot.session import DEFAULT_SESSION_ROOT, run_paper_session


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
        print(f"trade_env:    {settings.trade_env}")
        print(f"data_env:     {settings.data_env}")
        print(f"rest:         {settings.rest_base}")
        print(f"prod_ws:      {settings.prod_ws_url}")
        print(f"series:       {SERIES_TICKER_BTC_15M} — {series_info.get('title')}")
        print(f"demo creds:   {'present' if settings.has_credentials else 'missing (public-only)'}")
        print(f"prod creds:   {'present' if settings.has_prod_credentials else 'missing'}")
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


def cmd_record_btc_15m(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = require_prod_credentials(load_settings())
    paper = None
    if args.paper_price is not None:
        paper = PaperIntent(
            market_ticker="",
            outcome=args.paper_outcome,
            price=Decimal(args.paper_price),
            count=Decimal(args.paper_count),
            style=args.paper_style,
        )
    return asyncio.run(run_recorder(settings, seconds=args.seconds, paper=paper))


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


def build_record_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record-btc-15m",
        description="Record live production KXBTC15M WebSocket data (no production orders).",
    )
    parser.add_argument("--seconds", type=float, default=None, help="Stop after N seconds")
    parser.add_argument("--paper-outcome", choices=("yes", "no"), default="yes")
    parser.add_argument("--paper-price", default=None, help="Local paper limit in dollars")
    parser.add_argument("--paper-count", default="1")
    parser.add_argument("--paper-style", choices=("maker", "taker"), default="maker")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(cmd_discover_btc_15m(args))


def record_main(argv: list[str] | None = None) -> None:
    parser = build_record_parser()
    args = parser.parse_args(argv)
    raise SystemExit(cmd_record_btc_15m(args))


def build_dashboard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard-btc-15m",
        description="Local interactive view of the production KXBTC15M capture.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    return parser


def dashboard_main(argv: list[str] | None = None) -> None:
    parser = build_dashboard_parser()
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


def build_replay_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replay-btc-15m",
        description="Replay last-minute paper hints on a production KXBTC15M JSONL capture.",
    )
    parser.add_argument("--path", default=str(DEFAULT_JSONL), help="JSONL capture path")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    return parser


def cmd_replay_btc_15m(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"capture not found: {path}", file=sys.stderr)
        return 1
    settings = require_prod_credentials(load_settings())
    tickers = collect_tickers(path)
    with KalshiReadClient(settings) as rest:
        markets = fetch_markets(rest, tickers)
    report = replay(path, markets)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"windows: {payload['windows']}  traded: {payload['traded']}  "
          f"wins: {payload['wins']}  losses: {payload['losses']}  waits: {payload['waits']}")
    print(f"pnl: {payload['pnl']}  avg_pnl: {payload['avg_pnl']}")
    print()
    for row in payload["rows"]:
        mark = row["skip"] or row["side"] or "?"
        pnl = row["pnl"] if row["pnl"] is not None else "-"
        print(f"{row['ticker']}  {mark}  pnl={pnl}  "
              f"result={row['official_result']}  n={row['n']}  edge={row['edge']}")
    return 0


def replay_main(argv: list[str] | None = None) -> None:
    parser = build_replay_parser()
    args = parser.parse_args(argv)
    raise SystemExit(cmd_replay_btc_15m(args))


def build_paper_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-btc-15m",
        description=(
            "Paper-only KXBTC15M session. Tails the recorder JSONL and keeps a "
            "fake balance. Never sends orders. target-return is an aspirational KPI."
        ),
    )
    parser.add_argument("--bankroll", default="1000", help="Starting fake dollars")
    parser.add_argument("--hours", type=float, default=24, help="Session length")
    parser.add_argument(
        "--target-return",
        default="0.50",
        help="Aspirational return. 0.50 means plus 50 percent. Not a forecast.",
    )
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL), help="Recorder JSONL to tail")
    parser.add_argument(
        "--session-dir",
        default=str(DEFAULT_SESSION_ROOT),
        help="Where session state is written",
    )
    return parser


def cmd_paper_btc_15m(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    try:
        bankroll = Decimal(args.bankroll)
        target_return = Decimal(args.target_return)
    except InvalidOperation as exc:
        print(f"invalid bankroll or target-return: {exc}", file=sys.stderr)
        return 2
    try:
        return run_paper_session(
            bankroll=bankroll,
            hours=args.hours,
            target_return=target_return,
            jsonl_path=Path(args.jsonl),
            session_root=Path(args.session_dir),
            settings=settings,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def paper_main(argv: list[str] | None = None) -> None:
    parser = build_paper_parser()
    args = parser.parse_args(argv)
    raise SystemExit(cmd_paper_btc_15m(args))


if __name__ == "__main__":
    main()
