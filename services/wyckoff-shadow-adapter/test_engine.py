from datetime import UTC, datetime

import pandas as pd

from engine import SignalJournal, Thresholds, evaluate, raw_event, resample_closed


def minute_frame(periods=5000, start="2026-08-01T00:00:00Z"):
    opened = pd.date_range(start, periods=periods, freq="1min", tz="UTC")
    base = pd.Series(range(periods), dtype="float64") * 0.01 + 100.0
    return pd.DataFrame({
        "open_time": opened,
        "close_time": opened + pd.Timedelta(seconds=59),
        "open": base,
        "high": base + 0.2,
        "low": base - 0.2,
        "close": base + 0.05,
        "volume": 10.0,
        "quote_volume": 1000.0,
        "trade_count": 10,
    })


def test_resample_only_returns_complete_bars():
    frame = minute_frame(periods=17, start="2026-08-01T00:01:00Z")
    result = resample_closed(frame, "5min")
    assert len(result) == 2
    assert set(result["bars"]) == {5}


def test_detects_spring_and_upthrust():
    rows = pd.DataFrame({
        "open": [100.0] * 22,
        "high": [101.0] * 22,
        "low": [99.0] * 22,
        "close": [100.0] * 22,
        "volume": [10.0] * 22,
    })
    rows.loc[20, ["open", "high", "low", "close", "volume"]] = [99.5, 100.5, 98.0, 99.6, 20.0]
    assert raw_event(rows, 20, Thresholds()) == "SPRING"
    rows.loc[20, ["open", "high", "low", "close", "volume"]] = [100.5, 102.0, 99.5, 100.4, 20.0]
    assert raw_event(rows, 20, Thresholds()) == "UPTHRUST"


def test_signal_is_shadow_only_and_stale_data_rejects():
    frame = minute_frame()
    now = datetime(2026, 8, 10, tzinfo=UTC).timestamp()
    signal = evaluate(frame, "binance", "BTCUSDT", now=now)
    assert signal["mode"] == "SHADOW_ONLY"
    assert signal["liveTrading"] is False
    assert signal["executionActionable"] is False
    assert signal["executionGatePassed"] is False
    assert signal["authorityDecision"] == "REJECT"
    assert signal["authorityReason"] == "stale_market_data"


def test_journal_is_exactly_once(tmp_path):
    signal = evaluate(minute_frame(), "binance", "ETHUSDT", now=datetime(2026, 8, 4, 12, tzinfo=UTC).timestamp())
    journal = SignalJournal(tmp_path / "signals.db")
    assert journal.record(signal) is True
    assert journal.record(signal) is False
    assert journal.count() == 1
    flags = journal.connection.execute(
        "SELECT execution_actionable, execution_gate_passed FROM signals"
    ).fetchone()
    assert flags == (0, 0)
