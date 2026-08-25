import json
import time
from pathlib import Path

import pandas as pd

import engine
from engine import (FuturesConfig, FuturesEngine, ema_entry_gate_v2, liquidation_price,
                    position_pnl, transaction_tax)


def bars(up=True):
    now = pd.Timestamp.now(tz="UTC").floor("min")
    closes = [100 + i * (0.1 if up else -0.1) for i in range(40)]
    opened = pd.date_range(end=now, periods=40, freq="min")
    return pd.DataFrame({"open_time": opened, "close_time": opened + pd.Timedelta(seconds=59), "close": closes})


def wyckoff(path: Path, decision="WAIT", direction="NONE"):
    path.mkdir(parents=True)
    item = {"symbol":"BTCUSDT","barId":"bar-1","marketTime":pd.Timestamp.now(tz="UTC").isoformat(),"direction":direction,
            "authorityDecision":decision,"authorityReason":"test","stop":98,"target":104}
    (path / "latest-signals.json").write_text(json.dumps({"mode":"SHADOW_ONLY","liveTrading":False,"signals":[item]}))


def test_x10_liquidation_and_directional_pnl():
    assert liquidation_price(100, "LONG", 10, 0.005) == 90.5
    assert liquidation_price(100, "SHORT", 10, 0.005) == 109.5
    assert position_pnl("LONG", 10, 100, 101) == 10
    assert position_pnl("SHORT", 10, 100, 99) == 10


def test_sell_side_personal_income_tax_stress_model():
    assert transaction_tax("OPEN", "LONG", 1000, 10) == 0
    assert transaction_tax("CLOSE", "SHORT", 1000, 10) == 0
    assert transaction_tax("OPEN", "SHORT", 1000, 10) == 1
    assert transaction_tax("CLOSE", "LONG", 1000, 10) == 1
    assert transaction_tax("LIQUIDATION", "LONG", 1000, 10) == 1


def test_ema_entry_gate_v2_quantifies_closed_bar_quality_and_costs():
    allowed, reason, features = ema_entry_gate_v2(bars(True), FuturesConfig())
    assert allowed is True
    assert reason == "ema_gate_v2_pass"
    assert features["separationBps"] > 0
    assert features["directionalSlopeBps"] > 0
    assert round(features["netRiskReward"], 6) == 1.5


def test_ema_research_lock_records_decision_without_opening(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: bars(True))
    source = tmp_path / "wyckoff"; wyckoff(source)
    runner = FuturesEngine(Path("unused"), source, tmp_path / "data", "binance", ("BTCUSDT",),
                           FuturesConfig(ema_entry_enabled=False))
    state = runner.run_once()
    account = next(x for x in state["accounts"] if x["strategy"] == "ema_trend_x10")
    signal = next(x for x in state["recentSignals"] if x["strategy"] == "ema_trend_x10")
    assert account["positions"] == []
    assert account["summary"]["fills"] == 0
    assert signal["decision"] == "REJECTED"
    assert signal["reason"] == "strategy_research_lock:ema_gate_v2_pass"
    assert state["emaEntryMode"] == "RESEARCH_ONLY"


def test_long_round_trip_reports_gross_fees_tax_and_net(tmp_path):
    config = FuturesConfig(fee_bps=5, personal_income_tax_bps=10, slippage_bps=0)
    runner = FuturesEngine(Path("unused"), tmp_path / "wyckoff", tmp_path / "data", "binance", ("BTCUSDT",), config)
    runner._open("ema_trend_x10", "BTCUSDT", "LONG", 100, 99, 102)
    runner._close("ema_trend_x10", "BTCUSDT", 101, "CLOSE", "target")

    summary = runner.journal.summary("ema_trend_x10")
    trade = runner.journal.recent_closed_trades(1)[0]
    assert round(summary["gross_realized"], 6) == 10
    assert round(summary["fees"], 6) == 1.005
    assert round(summary["taxes"], 6) == 1.01
    assert round(summary["realized"], 6) == 7.985
    assert round(trade["gross_pnl"], 6) == 10
    assert round(trade["fee"], 6) == 1.005
    assert round(trade["tax"], 6) == 1.01
    assert round(trade["pnl"], 6) == 7.985


def test_ema_and_wyckoff_are_separate_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: bars(True))
    source = tmp_path / "wyckoff"; wyckoff(source, "ALLOW", "SHORT")
    runner = FuturesEngine(Path("unused"), source, tmp_path / "data", "binance", ("BTCUSDT",), FuturesConfig())
    state = runner.run_once()
    assert state["mode"] == "PAPER_FUTURES_ONLY" and state["liveTrading"] is False
    assert state["leverage"] == 10 and state["marginMode"] == "ISOLATED"
    accounts = {x["strategy"]: x for x in state["accounts"]}
    assert accounts["ema_trend_x10"]["positions"][0]["side"] == "LONG"
    assert accounts["wyckoff_x10"]["positions"][0]["side"] == "SHORT"
    assert accounts["ema_trend_x10"]["gross_notional"] > 900
    assert (tmp_path / "data" / "paper-futures.db").exists()
    assert state["recentClosedTrades"] == []


def test_wyckoff_wait_does_not_open(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: bars(True))
    source = tmp_path / "wyckoff"; wyckoff(source)
    runner = FuturesEngine(Path("unused"), source, tmp_path / "data", "binance", ("BTCUSDT",), FuturesConfig())
    state = runner.run_once(); accounts = {x["strategy"]: x for x in state["accounts"]}
    assert accounts["wyckoff_x10"]["positions"] == []
    assert state["recentSignals"][0]["decision"] in {"WAIT", "ALLOWED"}


def test_unsafe_wyckoff_projection_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: bars(True))
    source = tmp_path / "wyckoff"; source.mkdir(); (source / "latest-signals.json").write_text('{"mode":"LIVE","liveTrading":true}')
    runner = FuturesEngine(Path("unused"), source, tmp_path / "data", "binance", ("BTCUSDT",), FuturesConfig())
    try: runner.run_once()
    except ValueError as exc: assert str(exc) == "unsafe_wyckoff_projection"
    else: raise AssertionError("unsafe projection accepted")


def test_strategy_symbol_universes_are_separate(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "load_latest_bars", lambda _root, _exchange, symbol: seen.append(symbol) or bars(True))
    source = tmp_path / "wyckoff"
    source.mkdir()
    projection = {
        "mode": "SHADOW_ONLY", "liveTrading": False,
        "signals": [{"symbol": "SOLUSDT", "barId": "sol-1", "marketTime": pd.Timestamp.now(tz="UTC").isoformat(),
                     "direction": "LONG", "authorityDecision": "WAIT", "authorityReason": "warmup",
                     "stop": 98, "target": 104}],
    }
    (source / "latest-signals.json").write_text(json.dumps(projection))
    runner = FuturesEngine(
        Path("unused"), source, tmp_path / "data", "binance", ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        FuturesConfig(), ema_symbols=("BTCUSDT", "ETHUSDT"), wyckoff_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    )
    state = runner.run_once()
    ema_symbols = {item["symbol"] for item in state["recentSignals"] if item["strategy"] == "ema_trend_x10"}
    assert ema_symbols == {"BTCUSDT", "ETHUSDT"}
    assert "SOLUSDT" not in ema_symbols
    assert set(seen) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def test_wyckoff_warmup_is_wait_not_stale_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "load_latest_bars", lambda *_args, **_kwargs: bars(True))
    source = tmp_path / "wyckoff"
    source.mkdir()
    projection = {
        "mode": "SHADOW_ONLY", "liveTrading": False, "updatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "signals": [],
        "scheduler": [{"symbol": "SOLUSDT", "status": "WARMING_UP", "availableMinuteBars": 255,
                       "requiredMinuteBars": 1800}],
    }
    (source / "latest-signals.json").write_text(json.dumps(projection))
    runner = FuturesEngine(
        Path("unused"), source, tmp_path / "data", "binance", ("SOLUSDT",), FuturesConfig(),
        ema_symbols=(), wyckoff_symbols=("SOLUSDT",),
    )
    state = runner.run_once()
    signal = next(item for item in state["recentSignals"] if item["symbol"] == "SOLUSDT")
    assert signal["decision"] == "WAIT"
    assert signal["reason"] == "collecting_1800_closed_1m_bars"
    assert state["accounts"][1]["positions"] == []
