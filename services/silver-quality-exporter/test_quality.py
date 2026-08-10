from quality import Quality, classify, expected_minutes, parse_sources, timeframe_readiness


def quality(rows=6000, status=2, completeness=1.0):
    return Quality(0, 5999 * 60, 5999 * 60 + 59, rows, 6000, 6000 - rows,
                   100, 100, 0, 0, rows / 6000, completeness, 30, status)


def test_parse_sources_is_bounded_and_explicit():
    sources = parse_sources("binance:spot:websocket:BTCUSDT:kline_1m;bingx:spot:websocket:BTC-USDT:kline_1min")
    assert len(sources) == 2
    assert sources[1].symbol == "BTC-USDT"


def test_expected_minutes_includes_both_boundaries():
    assert expected_minutes(0, 0) == 1
    assert expected_minutes(0, 119) == 2


def test_status_thresholds_fail_closed():
    assert classify(30, 1.0, 120, 180) == 2
    assert classify(121, 1.0, 120, 180) == 1
    assert classify(181, 1.0, 120, 180) == 0
    assert classify(30, 0.97, 120, 180) == 0


def test_timeframes_are_only_derivable_with_sufficient_history():
    assert all(timeframe_readiness(quality()).values())
    limited = quality(rows=100)
    assert limited.completeness_total < 1
    assert timeframe_readiness(limited)["4h"] == 0
