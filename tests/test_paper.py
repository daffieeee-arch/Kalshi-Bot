"""Paper fills read the live book/tape, never a REST snapshot."""

from __future__ import annotations

from decimal import Decimal

from kalshi_bot.account import PaperAccount
from kalshi_bot.fees import quadratic_taker_fee
from kalshi_bot.orderbook import OrderbookState
from kalshi_bot.paper import PaperIntent, PaperLedger, net_open_qty
from test_orderbook import _delta, _snapshot


def test_taker_fills_when_implied_ask_crosses() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    # implied YES ask = 1 - 0.52 = 0.48
    ledger = PaperLedger()
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.50"),
        count=Decimal("3"),
        style="taker",
    )
    ledger.add(intent)
    fills = ledger.on_book(book, ts_ms=1)
    assert len(fills) == 1
    assert fills[0].price == Decimal("0.48")
    assert fills[0].count == Decimal("3")
    assert fills[0].fee > 0
    assert intent.remaining == Decimal("0")


def test_taker_does_not_fill_through_empty_ask() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot(no_dollars_fp=[]))
    ledger = PaperLedger()
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.99"),
        count=Decimal("1"),
        style="taker",
    )
    ledger.add(intent)
    assert ledger.on_book(book, ts_ms=1) == []
    assert intent.remaining == Decimal("1")


def test_maker_fills_only_on_tape_through_price() -> None:
    ledger = PaperLedger()
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("10"),
        style="maker",
    )
    ledger.add(intent)
    missed = ledger.on_trade(
        {
            "type": "trade",
            "sid": 3,
            "seq": 1,
            "msg": {
                "trade_id": "d91bc706-ee49-470d-82d8-11418bda6fed",
                "market_ticker": "KXBTC15M-TEST",
                "yes_price_dollars": "0.4100",
                "no_price_dollars": "0.5900",
                "count_fp": "5.00",
                "taker_side": "no",
                "taker_outcome_side": "no",
                "taker_book_side": "ask",
                "is_block_trade": False,
                "ts": 1,
                "ts_ms": 1000,
            },
        }
    )
    assert missed == []
    hits = ledger.on_trade(
        {
            "type": "trade",
            "sid": 3,
            "seq": 2,
            "msg": {
                "trade_id": "d91bc706-ee49-470d-82d8-11418bda6fee",
                "market_ticker": "KXBTC15M-TEST",
                "yes_price_dollars": "0.3900",
                "no_price_dollars": "0.6100",
                "count_fp": "5.00",
                "taker_side": "no",
                "taker_outcome_side": "no",
                "taker_book_side": "ask",
                "is_block_trade": False,
                "ts": 2,
                "ts_ms": 2000,
            },
        }
    )
    assert len(hits) == 1
    assert hits[0].price == Decimal("0.3900")
    assert hits[0].fee == Decimal("0")
    assert intent.remaining == Decimal("5")


def test_settlement_yes_when_close_avg_meets_strike() -> None:
    ledger = PaperLedger()
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("2"),
        style="maker",
    )
    ledger.add(intent)
    ledger.on_trade(
        {
            "type": "trade",
            "sid": 3,
            "seq": 2,
            "msg": {
                "trade_id": "d91bc706-ee49-470d-82d8-11418bda6fee",
                "market_ticker": "KXBTC15M-TEST",
                "yes_price_dollars": "0.4000",
                "no_price_dollars": "0.6000",
                "count_fp": "2.00",
                "taker_side": "no",
                "taker_outcome_side": "no",
                "taker_book_side": "ask",
                "is_block_trade": False,
                "ts": 2,
                "ts_ms": 2000,
            },
        }
    )
    ledger.on_settlement(
        market_ticker="KXBTC15M-TEST",
        close_avg=Decimal("86400"),
        floor_strike=Decimal("86300"),
    )
    assert intent.won is True
    assert intent.pnl == Decimal("2") - Decimal("0.80")


def test_taker_walks_visible_asks_without_overfill() -> None:
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.1000", "5.00"]],
            no_dollars_fp=[["0.6000", "2.00"], ["0.5000", "3.00"]],
        )
    )
    ledger = PaperLedger()
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.50"),
        count=Decimal("10"),
        style="taker",
    )
    ledger.add(intent)
    fills = ledger.on_book(book, ts_ms=1)
    assert sum((fill.count for fill in fills), Decimal("0")) == Decimal("5")
    assert intent.remaining == Decimal("5")
    assert [fill.price for fill in fills] == [Decimal("0.40"), Decimal("0.50")]
    second = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.99"),
        count=Decimal("10"),
        style="taker",
    )
    ledger.add(second)
    assert ledger.on_book(book, ts_ms=2) == []
    assert second.remaining == Decimal("10")


def test_taker_sell_hits_visible_bids_and_keeps_settlement_for_the_rest() -> None:
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.5000", "1.00"], ["0.4000", "8.00"]],
            no_dollars_fp=[["0.6000", "4.00"]],
        )
    )
    account = PaperAccount(Decimal("1000"))
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("2"),
        style="taker",
    )
    account.ledger.add(intent)
    buys = account.ledger.on_book(book, ts_ms=1)
    account.note_fills(buys)
    assert sum((fill.count for fill in buys), Decimal("0")) == Decimal("2")
    sells = account.ledger.sell_open(
        intent,
        book,
        ts_ms=2,
        min_price=Decimal("0.01"),
        reason="take_profit",
    )
    assert len(sells) == 1
    assert sells[0].action == "sell"
    assert sells[0].price == Decimal("0.50")
    assert sells[0].count == Decimal("1")
    assert sells[0].fee == quadratic_taker_fee(Decimal("1"), Decimal("0.50"))
    assert sells[0].fee > 0
    assert intent.status == "open"
    assert net_open_qty(intent) == Decimal("1")
    before_sell = account.cash
    account.note_fills(sells)
    assert account.cash == before_sell + sells[0].price * sells[0].count - sells[0].fee
    cash_after_sell = account.cash
    account.ledger.on_settlement(market_ticker="KXBTC15M-TEST", official_result="yes")
    paid = account.credit_settlements()
    assert paid == Decimal("1")
    assert intent.exit_reason == "settlement"
    assert account.cash == cash_after_sell + paid
    bought_cost = sum((fill.price * fill.count for fill in buys), Decimal("0"))
    bought_fee = sum((fill.fee for fill in buys), Decimal("0"))
    assert intent.pnl == paid + sells[0].price * sells[0].count - sells[0].fee - bought_cost - bought_fee


def test_full_sell_realizes_pnl_without_a_settlement_payout() -> None:
    book = OrderbookState()
    book.apply_snapshot(
        _snapshot(
            yes_dollars_fp=[["0.4500", "5.00"]],
            no_dollars_fp=[["0.6000", "5.00"]],
        )
    )
    account = PaperAccount(Decimal("1000"))
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("1"),
        style="taker",
    )
    account.ledger.add(intent)
    account.note_fills(account.ledger.on_book(book, ts_ms=1))
    sells = account.ledger.sell_open(
        intent,
        book,
        ts_ms=2,
        min_price=Decimal("0.01"),
        reason="stop",
    )
    account.note_fills(sells)
    assert intent.status == "closed"
    assert intent.exit_reason == "stop"
    assert intent.pnl is not None
    assert account.credit_settlements() == Decimal("0")
    assert account.equity(OrderbookState()) == Decimal("1000") + intent.pnl
    assert account.realized_pnl() == intent.pnl


def test_book_delta_does_not_fill_maker() -> None:
    book = OrderbookState()
    book.apply_snapshot(_snapshot())
    book.apply_delta(_delta(3))
    ledger = PaperLedger()
    intent = PaperIntent(
        market_ticker="KXBTC15M-TEST",
        outcome="yes",
        price=Decimal("0.40"),
        count=Decimal("1"),
        style="maker",
    )
    ledger.add(intent)
    assert ledger.on_book(book, ts_ms=1) == []
    assert intent.remaining == Decimal("1")
