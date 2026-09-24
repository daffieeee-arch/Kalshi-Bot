"""CLI entrypoints for discover (demo) and the production recorder."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
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
from kalshi_bot.session import (
    DEFAULT_SESSION_ROOT,
    ab_arm_dirs,
    paper_session_dirs,
    run_paper_session,
)


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
    parser.add_argument(
        "--session-dir",
        action="append",
        dest="session_dirs",
        default=None,
        help="Paper session root to show. Repeat for both forwardtest arms. Default includes ab-ref and ab-nolearn.",
    )
    return parser


def dashboard_main(argv: list[str] | None = None) -> None:
    parser = build_dashboard_parser()
    args = parser.parse_args(argv)
    if args.session_dirs:
        dirs = [Path(path) for path in args.session_dirs]
        primary = dirs[0]
    else:
        dirs = paper_session_dirs(DEFAULT_SESSION_ROOT)
        primary = DEFAULT_SESSION_ROOT
    uvicorn.run(
        create_app(session_dir=primary, session_dirs=dirs),
        host=args.host,
        port=args.port,
        log_level="info",
    )


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
    parser.add_argument(
        "--learn-exit-orders",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow learner.wants_exit to place exit orders. "
            "Default on, unless KALSHI_PAPER_LEARN_EXIT_ORDERS=0. "
            "Training and other exits stay on either way."
        ),
    )
    parser.add_argument(
        "--ab",
        action="store_true",
        help="Run REF and NO_LEARN_EXIT as two paper processes on the same JSONL.",
    )
    parser.add_argument(
        "--min-closes",
        type=int,
        default=None,
        help="After --hours, keep running until this many closes. The --ab default is 30.",
    )
    parser.add_argument(
        "--drawdown-stop",
        default=None,
        help="Stop when bankroll minus equity exceeds this many dollars. The --ab default is 25.",
    )
    return parser


def resolve_learn_exit_orders(
    cli_value: bool | None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """CLI wins. Otherwise the env var. Otherwise learned-exit orders stay enabled."""
    if cli_value is not None:
        return cli_value
    source = os.environ if environ is None else environ
    raw = source.get("KALSHI_PAPER_LEARN_EXIT_ORDERS")
    if raw is None or raw.strip() == "":
        return True
    norm = raw.strip().lower()
    if norm in {"1", "true", "yes", "on"}:
        return True
    if norm in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"KALSHI_PAPER_LEARN_EXIT_ORDERS={raw!r} is not 0 or 1"
    )


def paper_child_argv() -> list[str]:
    """Re-exec one paper arm. Never re-exec the ``--ab`` supervisor."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "paper-btc-15m" and argv0.is_file():
        return [str(argv0)]
    if argv0.name == "paper-btc-15m-ab":
        sibling = argv0.with_name("paper-btc-15m")
        if sibling.is_file():
            return [str(sibling)]
    return [sys.executable, "-m", "kalshi_bot.paper_cli"]


def ab_child_commands(
    *,
    bankroll: Decimal,
    hours: float,
    target_return: Decimal,
    jsonl_path: Path,
    session_root: Path,
    min_closes: int,
    drawdown_stop: Decimal,
) -> list[list[str]]:
    """Two processes, two directories, one shared capture. State does not cross."""
    ref_dir, no_dir = ab_arm_dirs(session_root)
    shared = [
        "--bankroll",
        format(bankroll, "f"),
        "--hours",
        str(hours),
        "--target-return",
        format(target_return, "f"),
        "--jsonl",
        str(jsonl_path),
        "--min-closes",
        str(min_closes),
        "--drawdown-stop",
        format(drawdown_stop, "f"),
    ]
    base = paper_child_argv()
    return [
        [*base, *shared, "--session-dir", str(ref_dir), "--learn-exit-orders"],
        [*base, *shared, "--session-dir", str(no_dir), "--no-learn-exit-orders"],
    ]


def run_paper_ab(
    *,
    bankroll: Decimal,
    hours: float,
    target_return: Decimal,
    jsonl_path: Path,
    session_root: Path,
    min_closes: int,
    drawdown_stop: Decimal,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> int:
    """Start REF and NO_LEARN_EXIT. A signal or a dead arm stops both."""
    commands = ab_child_commands(
        bankroll=bankroll,
        hours=hours,
        target_return=target_return,
        jsonl_path=jsonl_path,
        session_root=session_root,
        min_closes=min_closes,
        drawdown_stop=drawdown_stop,
    )
    print("forwardtest arms (paper only, capture stays up):")
    for command in commands:
        print(" ", " ".join(command))
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for command in commands:
            procs.append(popen(command, start_new_session=True))
    except Exception:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        raise
    stopping = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signum)

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while any(proc.poll() is None for proc in procs):
            if any(proc.poll() is not None for proc in procs):
                for proc in procs:
                    if proc.poll() is None:
                        proc.send_signal(signal.SIGTERM)
                break
            time.sleep(0.25)
        for proc in procs:
            if proc.poll() is None:
                proc.wait(timeout=30)
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    codes = [proc.returncode for proc in procs]
    if stopping:
        return 0
    return 0 if codes and all(code == 0 for code in codes) else 1


def _paper_amounts(args: argparse.Namespace) -> tuple[Decimal, Decimal, Decimal | None] | None:
    try:
        bankroll = Decimal(args.bankroll)
        target_return = Decimal(args.target_return)
        drawdown_stop = None if args.drawdown_stop is None else Decimal(args.drawdown_stop)
    except InvalidOperation as exc:
        print(f"invalid bankroll, target-return, or drawdown-stop: {exc}", file=sys.stderr)
        return None
    return bankroll, target_return, drawdown_stop


def cmd_paper_btc_15m(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    amounts = _paper_amounts(args)
    if amounts is None:
        return 2
    bankroll, target_return, drawdown_stop = amounts
    if args.ab and args.learn_exit_orders is not None:
        print("--ab sets both arms; omit --learn-exit-orders", file=sys.stderr)
        return 2
    if args.min_closes is not None and args.min_closes < 0:
        print("--min-closes must be >= 0", file=sys.stderr)
        return 2
    try:
        learn_exit_orders = resolve_learn_exit_orders(args.learn_exit_orders)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.ab:
        min_closes = 30 if args.min_closes is None else args.min_closes
        stop = Decimal("25") if drawdown_stop is None else drawdown_stop
        return run_paper_ab(
            bankroll=bankroll,
            hours=args.hours,
            target_return=target_return,
            jsonl_path=Path(args.jsonl),
            session_root=Path(args.session_dir),
            min_closes=min_closes,
            drawdown_stop=stop,
        )
    settings = load_settings()
    try:
        return run_paper_session(
            bankroll=bankroll,
            hours=args.hours,
            target_return=target_return,
            jsonl_path=Path(args.jsonl),
            session_root=Path(args.session_dir),
            settings=settings,
            learn_exit_orders=learn_exit_orders,
            min_closes=args.min_closes,
            drawdown_stop=drawdown_stop,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def paper_main(argv: list[str] | None = None) -> None:
    parser = build_paper_parser()
    args = parser.parse_args(argv)
    raise SystemExit(cmd_paper_btc_15m(args))


def ab_main(argv: Sequence[str] | None = None) -> None:
    """``paper-btc-15m-ab`` is ``paper-btc-15m --ab``."""
    parser = build_paper_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.learn_exit_orders is not None:
        print("--ab sets both arms; omit --learn-exit-orders", file=sys.stderr)
        raise SystemExit(2)
    args.ab = True
    raise SystemExit(cmd_paper_btc_15m(args))


if __name__ == "__main__":
    main()
