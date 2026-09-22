"""Subscription rollover keeps BRTI and switches only the market channels."""

from __future__ import annotations

import json

from kalshi_bot.recorder import LiveSubs, rollover_commands, select_active_market, static_subscriptions
from kalshi_bot.ws import CommandIds


def test_rollover_switches_ticker_and_leaves_brti_alone() -> None:
    state = LiveSubs(orderbook_sid=7, trade_sid=8, orderbook_ticker="KXBTC15M-A", trade_ticker="KXBTC15M-A")
    labeled = rollover_commands(state, "KXBTC15M-B", CommandIds())
    commands = [command for _, command in labeled]
    actions = [command["params"]["action"] for command in commands]
    assert actions == ["delete_markets", "delete_markets", "add_markets", "add_markets"]
    assert [command["params"]["market_tickers"][0] for command in commands] == [
        "KXBTC15M-A",
        "KXBTC15M-A",
        "KXBTC15M-B",
        "KXBTC15M-B",
    ]
    encoded = json.dumps(commands)
    assert "cfbenchmarks" not in encoded
    assert "BRTI" not in encoded
    assert state.orderbook_ticker == "KXBTC15M-B"
    assert state.trade_ticker == "KXBTC15M-B"


def test_first_ticker_subscribes_without_dropping_static_channels() -> None:
    state = LiveSubs()
    labeled = rollover_commands(state, "KXBTC15M-A", CommandIds())
    assert [purpose for purpose, _ in labeled] == ["orderbook", "trade"]
    assert labeled[0][1]["params"]["channels"] == ["orderbook_delta"]
    assert labeled[1][1]["params"]["channels"] == ["trade"]
    static = static_subscriptions(CommandIds())
    channels = [command["params"]["channels"][0] for command in static]
    assert channels == ["market_lifecycle_v2", "cfbenchmarks_value", "cfbenchmarks_value_5hz"]
    assert static[1]["params"]["index_ids"] == ["BRTI"]


def test_missing_market_does_not_raise() -> None:
    assert select_active_market([]) is None
    assert (
        select_active_market(
            [{"ticker": "KXBTC15M-OLD", "status": "closed", "close_time": "2099-01-01T00:00:00Z"}]
        )
        is None
    )


def test_reconnect_clears_book_separately_from_brti_plan() -> None:
    state = LiveSubs(orderbook_sid=1, trade_sid=2, orderbook_ticker="KXBTC15M-A", trade_ticker="KXBTC15M-A")
    state.orderbook_sid = None
    state.trade_sid = None
    state.orderbook_ticker = None
    state.trade_ticker = None
    labeled = rollover_commands(state, "KXBTC15M-A", CommandIds())
    assert all(command["cmd"] == "subscribe" for _, command in labeled)
    assert all("cfbenchmarks" not in json.dumps(command) for _, command in labeled)
