import pandas as pd

from walk_forward_wyckoff import evaluate


def test_evaluate_is_no_lookahead_and_cost_aware():
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z")
    signals = pd.DataFrame([{"signal_id": "a", "market_time": timestamp, "direction": "LONG",
                             "entry": 100.0, "stop": 99.0, "target": 102.0, "rr": 2.0}])
    bars = pd.DataFrame([
        {"open_time": timestamp, "close_time": timestamp, "open": 100, "high": 200, "low": 1, "close": 100},
        {"open_time": timestamp + pd.Timedelta(minutes=1), "close_time": timestamp + pd.Timedelta(minutes=1),
         "open": 100, "high": 102.1, "low": 99.5, "close": 102},
    ])
    result = evaluate(signals, bars, "oos", 200, 5, 10, 0)
    assert result.closed == 1 and result.wins == 1
    assert result.gross_pnl == 4.0
    assert result.fees > 0 and result.taxes > 0 and result.net_pnl < result.gross_pnl


def test_same_bar_stop_is_conservative():
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z")
    signals = pd.DataFrame([{"signal_id": "a", "market_time": timestamp, "direction": "LONG",
                             "entry": 100.0, "stop": 99.0, "target": 102.0, "rr": 2.0}])
    bars = pd.DataFrame([{"open_time": timestamp + pd.Timedelta(minutes=1), "close_time": timestamp,
                          "open": 100, "high": 103, "low": 98, "close": 100}])
    result = evaluate(signals, bars, "oos", 200, 5, 10, 0)
    assert result.closed == 1 and result.losses == 1
