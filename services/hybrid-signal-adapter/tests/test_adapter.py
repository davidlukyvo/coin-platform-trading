import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapter import Thresholds, evaluate


def bars(trending=True, trigger=True, volume=200.0):
    count = 80
    closes = [100 + i * 0.2 for i in range(count)] if trending else [100 - i * 0.1 for i in range(count)]
    highs = [value + 0.1 for value in closes]
    if trigger:
        closes[-1], highs[-1] = highs[-2] + 0.2, highs[-2] + 0.3
    else:
        closes[-1], highs[-1] = closes[-2], highs[-2]
    times = pd.date_range("2026-08-09T10:00:00Z", periods=count, freq="min")
    return pd.DataFrame({"open_time": times, "close_time": times + pd.Timedelta(seconds=59), "open": closes,
                         "high": highs, "low": [x - 0.2 for x in closes], "close": closes,
                         "volume": [100.0] * (count - 1) + [volume]})


def test_allow_is_shadow_and_never_execution_actionable():
    frame = bars()
    now = pd.Timestamp(frame.close_time.iloc[-1]).timestamp() + 1
    result = evaluate(frame, "binance", "BTCUSDT", now=now)
    assert result["authorityDecision"] == "ALLOW"
    assert result["finalAuthorityStatus"] == "READY"
    assert result["mode"] == "SHADOW_ONLY"
    assert result["executionActionable"] is False
    assert result["executionGatePassed"] is False


def test_waits_for_trigger_without_promoting_execution():
    frame = bars(trigger=False)
    now = pd.Timestamp(frame.close_time.iloc[-1]).timestamp() + 1
    result = evaluate(frame, "binance", "BTCUSDT", now=now)
    assert result["authorityDecision"] == "WAIT"
    assert "trigger_not_confirmed" in result["authorityTrace"]["blockers"]


def test_rejects_stale_market_data_fail_closed():
    frame = bars()
    now = pd.Timestamp(frame.close_time.iloc[-1]).timestamp() + 4000
    result = evaluate(frame, "binance", "BTCUSDT", Thresholds(max_market_age_seconds=3900), now=now)
    assert result["authorityDecision"] == "REJECT"
    assert result["authorityReason"] == "stale_market_data"


def test_rejects_unaligned_trend():
    frame = bars(trending=False, trigger=False)
    now = pd.Timestamp(frame.close_time.iloc[-1]).timestamp() + 1
    result = evaluate(frame, "binance", "ETHUSDT", now=now)
    assert result["authorityDecision"] == "REJECT"
    assert "trend_not_aligned" in result["authorityTrace"]["blockers"]
