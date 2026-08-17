import pandas as pd

from engine import Journal, Thresholds, evaluate_bar, is_pivot, resample_5m


def bars(values):
    values = [float(value) for value in values]
    opened = pd.date_range("2026-01-01", periods=len(values), freq="5min", tz="UTC")
    return pd.DataFrame({"open_time": opened, "close_time": opened + pd.Timedelta(minutes=5),
                         "open": values, "high": [x + 1 for x in values], "low": [x - 1 for x in values],
                         "close": values, "volume": 100.0, "minute_bars": 5})


def test_pivot_requires_right_hand_confirmation():
    frame = bars([10, 11, 14, 11, 10, 9, 10])
    assert is_pivot(frame, 2, 2, 2, True)
    assert not is_pivot(frame.iloc[:4].reset_index(drop=True), 2, 2, 2, True)


def test_resample_accepts_only_complete_five_minute_bars():
    opened = pd.date_range("2026-01-01", periods=9, freq="min", tz="UTC")
    source = pd.DataFrame({"open_time": opened, "close_time": opened + pd.Timedelta(seconds=59),
                           "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1})
    result = resample_5m(source)
    assert len(result) == 1 and result.iloc[0]["minute_bars"] == 5


def test_sweep_mss_ote_sequence_is_shadow_only():
    frame = bars([100] * 30)
    state = {"bsl": 102.0, "ssl": 98.0}
    frame.loc[25, ["low", "close"]] = [97.0, 99.0]
    state, sweep = evaluate_bar(frame, 25, "BTCUSDT", "binance", state, {}, Thresholds(), now=frame.iloc[25].close_time.timestamp())
    assert sweep["liquiditySweep"] == "SSL_SWEEP"
    frame.loc[26, ["open", "high", "close"]] = [101.0, 104.0, 103.0]
    state, mss = evaluate_bar(frame, 26, "BTCUSDT", "binance", state, {}, Thresholds(), now=frame.iloc[26].close_time.timestamp())
    assert mss["marketStructureShift"] == "BULLISH_MSS" and state["ote"]["direction"] == "LONG"
    frame.loc[27, ["open", "high", "low", "close"]] = [100.0, 101.0, 98.0, 100.5]
    state, signal = evaluate_bar(frame, 27, "BTCUSDT", "binance", state, {}, Thresholds(), now=frame.iloc[27].close_time.timestamp())
    assert signal["decision"] == "ALLOW_SHADOW" and signal["direction"] == "LONG"
    assert signal["liveTrading"] is False and signal["executionActionable"] is False
    assert signal["executionGatePassed"] is False


def test_journal_is_exactly_once(tmp_path):
    journal = Journal(tmp_path)
    observation = {"observationId": "fixed", "marketTime": "2026-01-01T00:00:00+00:00"}
    assert journal.save("BTCUSDT", {}, observation)
    assert not journal.save("BTCUSDT", {}, observation)
    assert journal.rows() == 1
