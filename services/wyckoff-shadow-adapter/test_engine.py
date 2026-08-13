from datetime import UTC, datetime

import pandas as pd

from engine import (SignalJournal, Thresholds, WyckoffShadowAdapter, classify_market_phase,
                    detect_vsa_events, evaluate, raw_event, resample_closed)


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


def test_vsa_no_demand_is_quantified_and_explained():
    rows = pd.DataFrame({"open": [100.0] * 22, "high": [101.0] * 22, "low": [99.0] * 22,
                         "close": [100.0] * 22, "volume": [10.0] * 22})
    rows.loc[21, ["open", "high", "low", "close", "volume"]] = [100.0, 100.3, 99.9, 100.1, 5.0]
    events = detect_vsa_events(rows, Thresholds())
    assert events[0]["event"] == "NO_DEMAND"
    assert events[0]["direction"] == "BEARISH"
    assert 0 <= events[0]["score"] <= 100
    assert events[0]["explanation"]


def test_four_primary_market_phases_are_explicit():
    assert classify_market_phase(.8, 111, 90, 110, "BULLISH", 1, set())[0] == "MARKUP"
    assert classify_market_phase(.2, 89, 90, 110, "BEARISH", -1, set())[0] == "MARKDOWN"
    assert classify_market_phase(.4, 98, 90, 110, "BEARISH", 0, {"NO_SUPPLY"})[0] == "ACCUMULATION"
    assert classify_market_phase(.7, 104, 90, 110, "BULLISH", 0, {"UPTHRUST"})[0] == "DISTRIBUTION"


def test_signal_is_shadow_only_and_stale_data_rejects():
    frame = minute_frame()
    now = datetime(2026, 8, 10, tzinfo=UTC).timestamp()
    signal = evaluate(frame, "binance", "BTCUSDT", now=now)
    assert signal["mode"] == "SHADOW_ONLY"
    assert signal["liveTrading"] is False
    assert signal["executionActionable"] is False
    assert signal["executionGatePassed"] is False
    assert signal["phaseCandidate"] in {"ACCUMULATION", "MARKUP", "DISTRIBUTION", "MARKDOWN"}
    assert signal["strategy"] == "wyckoff_vsa_research_v1"
    assert signal["authorityDecision"] == "REJECT"
    assert signal["authorityReason"] == "stale_market_data"


def test_journal_is_exactly_once(tmp_path):
    signal = evaluate(minute_frame(), "binance", "ETHUSDT", now=datetime(2026, 8, 4, 12, tzinfo=UTC).timestamp())
    journal = SignalJournal(tmp_path / "signals.db")
    assert journal.record(signal) is True
    assert journal.record(signal) is False
    assert journal.count() == 1
    assert journal.research_count() == 1
    flags = journal.connection.execute(
        "SELECT execution_actionable, execution_gate_passed FROM signals"
    ).fetchone()
    assert flags == (0, 0)


def test_scheduler_processes_every_unseen_closed_5m_bar_in_order(tmp_path, monkeypatch):
    source = {"frame": minute_frame(periods=4000)}
    monkeypatch.setattr("engine.load_closed_minutes", lambda *_args, **_kwargs: source["frame"].copy())
    adapter = WyckoffShadowAdapter(tmp_path, tmp_path / "out", "binance", ("BTCUSDT",), Thresholds())
    first = adapter.run_once()
    assert first["researchJournalRows"] == 1

    source["frame"] = minute_frame(periods=4015)
    second = adapter.run_once()
    rows = adapter.journal.connection.execute(
        "SELECT market_time FROM research_observations WHERE symbol='BTCUSDT' ORDER BY market_time"
    ).fetchall()
    timestamps = [pd.Timestamp(row[0]) for row in rows]
    assert len(timestamps) == 4
    assert all((right - left) == pd.Timedelta("5min") for left, right in zip(timestamps, timestamps[1:]))
    assert second["scheduler"][0]["processedBars"] == 3
    assert second["scheduler"][0]["backlogBars"] == 0
    assert second["scheduler"][0]["missingResearchBars"] == 0


def test_scheduler_catchup_cap_does_not_skip_to_latest_bar(tmp_path, monkeypatch):
    source = {"frame": minute_frame(periods=4000)}
    monkeypatch.setattr("engine.load_closed_minutes", lambda *_args, **_kwargs: source["frame"].copy())
    adapter = WyckoffShadowAdapter(tmp_path, tmp_path / "out", "binance", ("BTCUSDT",), Thresholds(), 2)
    adapter.run_once()
    source["frame"] = minute_frame(periods=4020)
    result = adapter.run_once()
    assert result["scheduler"][0]["processedBars"] == 2
    assert result["scheduler"][0]["backlogBars"] == 2
    assert adapter.journal.research_count() == 3
