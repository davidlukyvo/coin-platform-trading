import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

import engine
from engine import Journal, OrderIntent, PaperBroker, PaperEngine, RiskEngine, RiskLimits


def intent(**changes):
    values = dict(signal_id="signal-1", symbol="BTCUSDT", side="BUY", notional=100.0, market_price=50000.0, market_time=time.time())
    values.update(changes)
    return OrderIntent(**values)


def evaluate(risk, order=None, **changes):
    context = dict(equity=10000.0, peak_equity=10000.0, day_start_equity=10000.0, symbol_exposure=0.0, gross_exposure=0.0, trades_today=0, duplicate=False)
    context.update(changes)
    return risk.evaluate(order or intent(), **context)


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        ({"duplicate": True}, "duplicate_signal"),
        ({"gross_exposure": 200.0}, "max_gross_exposure"),
        ({"symbol_exposure": 60.0}, "max_symbol_position"),
        ({"equity": 9975.0}, "daily_loss_limit"),
        ({"equity": 9800.0, "day_start_equity": 9800.0}, "drawdown_limit"),
        ({"trades_today": 20}, "max_trades_per_day"),
    ],
)
def test_risk_rejections(context, reason):
    assert evaluate(RiskEngine(RiskLimits()), **context).reason == reason


def test_risk_kill_switch_and_stale_market():
    assert evaluate(RiskEngine(RiskLimits(), kill_switch=True)).reason == "kill_switch"
    stale = intent(market_time=time.time() - 301)
    assert evaluate(RiskEngine(RiskLimits()), stale).reason == "stale_market_data"


def test_risk_allows_bounded_order():
    decision = evaluate(RiskEngine(RiskLimits()))
    assert decision.allowed and decision.reason == "allowed"


def test_risk_rejects_oversized_order():
    assert evaluate(RiskEngine(RiskLimits()), intent(notional=100.01)).reason == "max_order_notional"


def test_risk_allows_exit_above_entry_limit_when_it_only_reduces_position():
    decision = evaluate(
        RiskEngine(RiskLimits()),
        intent(side="SELL", notional=100.25),
        symbol_exposure=100.25,
        gross_exposure=100.25,
    )
    assert decision.allowed and decision.reason == "allowed_risk_reducing_exit"


@pytest.mark.parametrize("exposure", [0.0, 50.0])
def test_risk_rejects_sell_that_exceeds_position(exposure):
    decision = evaluate(
        RiskEngine(RiskLimits()),
        intent(side="SELL", notional=100.0),
        symbol_exposure=exposure,
        gross_exposure=exposure,
    )
    assert not decision.allowed and decision.reason == "sell_exceeds_position"


def test_risk_reducing_exit_still_honors_kill_switch_and_stale_data():
    sell = intent(side="SELL", notional=100.25)
    context = {"symbol_exposure": 100.25, "gross_exposure": 100.25}
    assert evaluate(RiskEngine(RiskLimits(), kill_switch=True), sell, **context).reason == "kill_switch"
    stale = intent(side="SELL", notional=100.25, market_time=time.time() - 301)
    assert evaluate(RiskEngine(RiskLimits()), stale, **context).reason == "stale_market_data"


def test_paper_fill_models_fee_slippage_and_roundtrip(tmp_path):
    journal = Journal(tmp_path / "paper.db", 10000)
    broker = PaperBroker(journal, fee_bps=4, slippage_bps=2)
    buy = broker.fill(intent(signal_id="buy", market_price=100, notional=100))
    assert buy["price"] > 100
    assert buy["fee"] > 0
    position = journal.positions()["BTCUSDT"]
    sell_notional = float(position["quantity"]) * 99
    sell = broker.fill(intent(signal_id="sell", side="SELL", market_price=99, notional=sell_notional))
    assert sell["price"] < 99
    assert sell["realized_pnl"] < 0
    assert journal.trades_today() == 2
    summary = journal.summary()
    assert summary["orders_total"] == summary["fills_total"] == 2
    assert summary["closed_trades"] == 1
    assert summary["losing_trades"] == 1
    assert summary["fees_total"] > 0


def synthetic_bars(up=True):
    now = pd.Timestamp.now(tz="UTC").floor("min")
    closes = [100 + i * (1 if up else -0.2) for i in range(40)]
    open_times = pd.date_range(end=now, periods=40, freq="min")
    close_times = pd.DatetimeIndex([item + timedelta(seconds=59) for item in open_times])
    return pd.DataFrame({"open_time": open_times, "close_time": close_times, "close": closes})


def test_engine_is_exactly_once_for_same_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: synthetic_bars(True))
    paper = PaperEngine(Path("unused"), tmp_path, symbols=("BTCUSDT",), starting_cash=10000, target_notional=100)
    first = paper.run_once()
    second = paper.run_once()
    assert len(first["recent_fills"]) == 1
    assert len(second["recent_fills"]) == 1
    assert first["mode"] == "PAPER_ONLY" and first["live_trading"] is False
    assert first["summary"]["signals_total"] == 1
    assert first["summary"]["fills_total"] == 1
    assert first["recent_signals"][0]["decision"] == "ALLOWED"
    assert first["starting_equity"] == 10000
    assert (tmp_path / "latest-state.json").exists()


def test_engine_writes_ui_projection_outside_private_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: synthetic_bars(True))
    journal_root = tmp_path / "journal"
    state_root = tmp_path / "ui"
    paper = PaperEngine(
        Path("unused"), journal_root, state_root=state_root,
        symbols=("BTCUSDT",), starting_cash=10000, target_notional=100,
    )
    paper.run_once()
    assert (journal_root / "paper.db").exists()
    assert not (journal_root / "latest-state.json").exists()
    assert (state_root / "latest-state.json").exists()


def test_engine_can_run_periodic_cycle_from_scheduler_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: synthetic_bars(True))
    paper = PaperEngine(Path("unused"), tmp_path, symbols=("BTCUSDT",))
    paper.run_once()
    with ThreadPoolExecutor(max_workers=1) as executor:
        state = executor.submit(paper.run_once).result()
    assert state["mode"] == "PAPER_ONLY"
    assert len(state["recent_fills"]) == 1


def test_kill_switch_journals_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: synthetic_bars(True))
    paper = PaperEngine(Path("unused"), tmp_path, symbols=("BTCUSDT",), kill_switch=True)
    state = paper.run_once()
    assert state["recent_fills"] == []
    assert state["recent_risk_events"][0]["reason"] == "kill_switch"
