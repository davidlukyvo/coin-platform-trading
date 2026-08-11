import json
import time
from pathlib import Path

import pandas as pd

import engine
from engine import FuturesConfig, FuturesEngine, liquidation_price, position_pnl


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
