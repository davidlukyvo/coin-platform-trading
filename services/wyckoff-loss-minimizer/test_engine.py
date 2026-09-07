import json
import sqlite3

import pandas as pd

from engine import ResearchConfig, analyze, outcomes, persist


def sample_signals(count=50):
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    return pd.DataFrame([{"signal_id": str(i), "market_time": start + pd.Timedelta(hours=i),
        "phase": "DISTRIBUTION" if i % 2 else "ACCUMULATION", "event": "UPTHRUST" if i % 2 else "SPRING",
        "direction": "SHORT" if i % 2 else "LONG", "score": 80, "entry": 100.0,
        "stop": 101.0 if i % 2 else 99.0, "target": 98.0 if i % 2 else 102.0, "rr": 2.0,
        "relativeVolume15m": 1.5, "spreadRatio15m": 1.4, "hourlyContext": "BEARISH" if i % 2 else "BULLISH",
        "hourlyEMA20Slope": -1 if i % 2 else 1, "contextAligned": True} for i in range(count)])


def sample_bars():
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    return pd.DataFrame([{"open_time": start + pd.Timedelta(minutes=i), "high": 102.1,
                          "low": 97.9, "close": 100.0} for i in range(60*55)])


def test_stop_first_and_costs_are_conservative():
    result = outcomes(sample_signals(1), sample_bars(), ResearchConfig()).iloc[0]
    assert result.exit_reason == "STOP"
    assert result.net_pnl < result.gross_pnl


def test_no_candidate_qualifies_without_positive_all_segments():
    report = analyze(sample_signals(), sample_bars(), ResearchConfig())
    assert report["mode"] == "SHADOW_RESEARCH_ONLY"
    assert report["executionActionable"] is False
    assert report["recommendation"] == "NO_TRADE_RESEARCH_LOCK"
    assert report["qualifiedCandidates"] == []


def test_persist_is_exactly_once(tmp_path):
    report = {"reportId": "abc"}
    assert persist(report, tmp_path) is True
    assert persist(report, tmp_path) is False
    assert len((tmp_path / "reports.jsonl").read_text().splitlines()) == 1
