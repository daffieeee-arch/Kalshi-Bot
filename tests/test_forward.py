"""Learned-exit order gate, exit deltas, and the two-arm paper forwardtest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_bot.cli import (
    ab_child_commands,
    build_paper_parser,
    resolve_learn_exit_orders,
    run_paper_ab,
)
from kalshi_bot.dashboard.app import create_app
from kalshi_bot.dashboard.feed import LiveFeed
from kalshi_bot.exit_audit import brier_report, build_delta, stamp_settlement
from kalshi_bot.learn import MIN_INFLUENCE
from kalshi_bot.session import PaperSession, ab_arm_dirs
from test_session import _IN_WINDOW, _arm_quotes, _held_yes, _session


class _Done:
    def __init__(self) -> None:
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def send_signal(self, _signum: int) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def test_learn_exit_orders_default_on_and_env_or_cli() -> None:
    args = build_paper_parser().parse_args([])
    assert args.learn_exit_orders is None
    assert args.min_closes is None
    assert args.drawdown_stop is None
    assert resolve_learn_exit_orders(None, {}) is True
    assert resolve_learn_exit_orders(None, {"KALSHI_PAPER_LEARN_EXIT_ORDERS": "0"}) is False
    assert resolve_learn_exit_orders(None, {"KALSHI_PAPER_LEARN_EXIT_ORDERS": "1"}) is True
    assert resolve_learn_exit_orders(True, {"KALSHI_PAPER_LEARN_EXIT_ORDERS": "0"}) is True
    off = build_paper_parser().parse_args(["--no-learn-exit-orders"])
    assert off.learn_exit_orders is False
    try:
        resolve_learn_exit_orders(None, {"KALSHI_PAPER_LEARN_EXIT_ORDERS": "maybe"})
    except ValueError as exc:
        assert "0 or 1" in str(exc)
    else:
        raise AssertionError("expected a bad env value to fail")


def test_gate_blocks_only_learned_exit_orders(tmp_path: Path) -> None:
    blocked = _session(tmp_path / "off")
    blocked.learn_exit_orders = False
    blocked.learner.n = MIN_INFLUENCE
    blocked.learner.bias = -3.0
    intent = _held_yes(blocked, price="0.40")
    _arm_quotes(blocked, spot="200000", strike="100", yes_bid="0.4200", no_bid="0.5000")
    blocked._on_quote(500, _IN_WINDOW)
    assert intent.status == "open"
    assert intent.exit_reason is None
    assert blocked.learner.n == MIN_INFLUENCE
    assert intent.entry_quote is not None
    assert intent.entry_quote["predicates"]["learned"] is True

    allowed = _session(tmp_path / "on")
    allowed.learner.n = MIN_INFLUENCE
    allowed.learner.bias = -3.0
    closed = _held_yes(allowed, price="0.40")
    _arm_quotes(allowed, spot="200000", strike="100", yes_bid="0.4200", no_bid="0.5000")
    allowed._on_quote(500, _IN_WINDOW)
    assert closed.status == "closed"
    assert closed.exit_reason == "learned"
    assert allowed.learner.n == MIN_INFLUENCE + 1


def test_other_exits_still_train_when_learned_orders_are_off(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.learn_exit_orders = False
    session.arm = "no_learn_exit"
    intent = _held_yes(session, price="0.40")
    _arm_quotes(session, spot="100", strike="200000", yes_bid="0.4200", no_bid="0.5000")
    session._on_quote(400, _IN_WINDOW)
    assert intent.exit_reason == "signal_flip"
    assert session.learner.n == 1
    assert intent.exit_delta is not None
    assert intent.exit_delta["sub_1s"] is True
    assert intent.exit_delta["hold_ms"] == 399
    assert intent.exit_delta["flip_already_true_at_entry"] is True
    assert intent.exit_delta["same_quote_contradiction"] is True
    assert intent.exit_delta["learn_exit_orders"] is False
    assert "model_p_delta" in intent.exit_delta
    assert "feature_delta" in intent.exit_delta
    session.save()
    loaded = PaperSession.load(session.root)
    assert loaded is not None
    assert loaded.account.ledger.intents[0].exit_delta["same_quote_contradiction"] is True
    metrics_path = session.root / session.session_id / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["closes"] == 1
    assert metrics["sub_1s_closes"] == 1
    assert metrics["exit_reasons"]["signal_flip"] == 1
    assert metrics["influences"]["training_on_closes"] is True
    assert metrics["influences"]["learn_exit_orders"] is False
    assert metrics["influences"]["signal_flip"] is True
    assert metrics["influences"]["adjust_params"] is True
    assert "not evidence of a better prediction" in metrics["activity_note"]


def test_legacy_state_keeps_learned_exit_orders(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.save()
    state_path = session.root / session.session_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    del payload["learn_exit_orders"]
    del payload["arm"]
    del payload["finished"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = PaperSession.load(session.root)
    assert loaded is not None
    assert loaded.arm == "legacy"
    assert loaded.learn_exit_orders is True
    assert loaded.finished_known is False


def test_drawdown_and_min_closes_are_locked_on_the_session(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.min_closes = 30
    session.drawdown_stop = Decimal("25")
    session.ends_at = _IN_WINDOW
    assert session._schedule_done(_IN_WINDOW) is False
    session.account.cash = Decimal("970")
    assert session._drawdown_hit() is True
    session.account.cash = Decimal("980")
    assert session._drawdown_hit() is False


def test_brier_uses_settlement_and_market_benchmark() -> None:
    rows = [
        {
            "model_yes": "0.80",
            "yes_mid": "0.60",
            "yes_ask": "0.62",
            "settlement_yes": 1,
            "settlement_source": "official",
        },
        {
            "model_yes": "0.20",
            "yes_mid": "0.40",
            "yes_ask": "0.42",
            "settlement_yes": 0,
            "settlement_source": "official",
        },
    ]
    report = brier_report(rows)
    assert report["model"] == "signal.model_yes"
    assert report["label"] == "settlement_yes"
    assert report["n"] == 2
    assert report["brier_constant_50"] == 0.25
    assert report["brier_model"] is not None
    assert report["brier_model"] < report["brier_constant_50"]
    assert report["brier_market"] is not None
    assert report["accuracy_model_at_50"] == 1.0
    assert "not edge proof" in report["note"]
    empty = brier_report([{"model_yes": "0.5", "settlement_yes": None}])
    assert empty["n"] == 0
    assert empty["brier_model"] is None
    mixed = [
        {
            "ticker": "KXBTC15M-TEST",
            "model_yes": "0.20",
            "yes_mid": "0.40",
            "settlement_yes": 0,
            "settlement_source": "reconstructed",
        }
    ]
    stamp_settlement(mixed, "KXBTC15M-TEST", yes_won=True, source="reconstructed")
    assert mixed[0]["settlement_yes"] == 0
    stamp_settlement(mixed, "KXBTC15M-TEST", yes_won=True, source="official")
    assert mixed[0]["settlement_source"] == "official"
    assert mixed[0]["settlement_yes"] == 1
    stamp_settlement(mixed, "KXBTC15M-TEST", yes_won=False, source="reconstructed")
    assert mixed[0]["settlement_yes"] == 1


def test_delta_names_the_predicate_that_flipped() -> None:
    entry = {"model_p": "0.70", "predicates": {"signal_flip": False, "stop": False}, "features": [0.1, 0.0]}
    exit_mark = {"model_p": "0.40", "predicates": {"signal_flip": True, "stop": False}, "features": [0.0, 1.0]}
    delta = build_delta(
        entry,
        exit_mark,
        reason="signal_flip",
        hold_ms=50,
        learn_exit_orders=True,
        learned_signal=False,
    )
    assert delta["predicate_flipped_true"] == ["signal_flip"]
    assert delta["model_p_delta"] == "-0.30"
    assert delta["sub_1s"] is True
    assert delta["same_quote_contradiction"] is False
    assert delta["feature_delta"]["edge"] == -0.1


def test_ab_commands_use_isolated_session_dirs(tmp_path: Path) -> None:
    root = tmp_path / "paper-sessions"
    ref_dir, no_dir = ab_arm_dirs(root)
    assert ref_dir != root
    assert no_dir != root
    commands = ab_child_commands(
        bankroll=Decimal("1000"),
        hours=24,
        target_return=Decimal("0.50"),
        jsonl_path=Path("data/prod-kxbtc15m.jsonl"),
        session_root=root,
        min_closes=30,
        drawdown_stop=Decimal("25"),
    )
    assert commands[0][commands[0].index("--session-dir") + 1] == str(ref_dir)
    assert commands[1][commands[1].index("--session-dir") + 1] == str(no_dir)
    assert "--learn-exit-orders" in commands[0]
    assert "--no-learn-exit-orders" in commands[1]
    assert "--ab" not in commands[0]
    assert "--ab" not in commands[1]

    calls: list[list[str]] = []

    def popen(command: list[str], start_new_session: bool = False) -> _Done:
        assert start_new_session is True
        calls.append(command)
        return _Done()

    code = run_paper_ab(
        bankroll=Decimal("1000"),
        hours=24,
        target_return=Decimal("0.50"),
        jsonl_path=Path("data/prod-kxbtc15m.jsonl"),
        session_root=root,
        min_closes=30,
        drawdown_stop=Decimal("25"),
        popen=popen,
    )
    assert code == 0
    assert len(calls) == 2


def test_dashboard_lists_both_arms(tmp_path: Path) -> None:
    ref = PaperSession.create(
        tmp_path / "ab-ref",
        bankroll=Decimal("1000"),
        hours=24,
        target_return=Decimal("0.50"),
        now=_IN_WINDOW,
        learn_exit_orders=True,
    )
    off = PaperSession.create(
        tmp_path / "ab-nolearn",
        bankroll=Decimal("1000"),
        hours=24,
        target_return=Decimal("0.50"),
        now=_IN_WINDOW,
        learn_exit_orders=False,
    )
    ref.save()
    off.save()
    jsonl = tmp_path / "cap.jsonl"
    jsonl.write_text("", encoding="utf-8")
    feed = LiveFeed(
        jsonl_path=jsonl,
        lock_path=tmp_path / "recorder.lock",
        session_dir=ref.root,
        session_dirs=[ref.root, off.root],
    )
    snap = feed.snapshot()
    assert [row["arm_label"] for row in snap["paper_sessions"]] == ["REF", "NO_LEARN_EXIT"]
    assert snap["paper_sessions"][1]["learn_exit_orders"] is False
    assert snap["paper_sessions"][1]["learner_influences"]["stop"] is True
    app = create_app(
        jsonl_path=jsonl,
        lock_path=tmp_path / "dash.lock",
        session_dir=ref.root,
        session_dirs=[ref.root, off.root],
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="paper-arms"' in page.text
        assert 'id="paper-variant"' in page.text
        body = client.get("/api/state").json()
        assert len(body["paper_sessions"]) == 2
        assert body["paper_sessions"][0]["metrics"]["net_pnl"] == "0"
