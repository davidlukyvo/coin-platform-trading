import numpy as np
import pandas as pd
from research import backtest, features, signal


def bars(n=500):
    close = 100 * np.cumprod(1 + np.sin(np.arange(n)/20) * .0005)
    return pd.DataFrame({"open_time": pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC"),
                         "open": close, "high": close*1.001, "low": close*.999, "close": close, "volume": 1.0})


def test_features_are_causal():
    a = features(bars()); changed = bars(); changed.loc[400:, "close"] *= 2; b = features(changed)
    pd.testing.assert_frame_equal(a.iloc[:399], b.iloc[:399])


def test_signal_executes_next_bar():
    f = features(bars())
    raw = (f.ema_fast > f.ema_slow).astype(float)
    pd.testing.assert_series_equal(signal(f, "ema_trend").iloc[1:].reset_index(drop=True), raw.iloc[:-1].reset_index(drop=True), check_names=False)


def test_costs_reduce_return():
    df = bars()
    free = backtest(df, "BTCUSDT", "ema_trend", "test", 0, 0)
    costly = backtest(df, "BTCUSDT", "ema_trend", "test", 10, 5)
    assert costly.net_return <= free.net_return


def test_short_history_is_exploratory():
    assert backtest(bars(), "ETHUSDT", "rsi_reversion", "oos").confidence == "EXPLORATORY"
