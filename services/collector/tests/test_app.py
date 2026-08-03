from datetime import datetime, timezone
from pathlib import Path

import pytest

import app


def make_settings(raw_root: Path, metrics_port: int = 8000) -> app.Settings:
    return app.Settings(
        ws_base="wss://example.test",
        symbols=("btcusdt",),
        streams=("aggTrade",),
        raw_root=raw_root,
        quarantine_root=raw_root / "quarantine",
        metrics_port=metrics_port,
        queue_size=10,
        stale_after_seconds=60,
        reconnect_max_seconds=60,
        write_max_attempts=3,
    )


def test_stream_names_builds_supported_streams() -> None:
    assert app.stream_names(("btcusdt",), ("aggTrade", "kline_1m")) == [
        "btcusdt@aggTrade",
        "btcusdt@kline_1m",
    ]


def test_stream_names_rejects_unsupported_stream() -> None:
    with pytest.raises(ValueError, match="Unsupported stream"):
        app.stream_names(("btcusdt",), ("depth",))


def test_parse_expected_stream_accepts_configured_stream() -> None:
    assert app.parse_expected_stream("btcusdt@aggTrade", frozenset({"btcusdt@aggTrade"})) == (
        "BTCUSDT",
        "aggTrade",
    )


def test_parse_expected_stream_rejects_unexpected_stream() -> None:
    with pytest.raises(ValueError, match="unexpected stream"):
        app.parse_expected_stream("attacker@unexpected", frozenset({"btcusdt@aggTrade"}))


def test_partition_path_uses_utc_date_and_hour(tmp_path: Path) -> None:
    event_time = datetime(2026, 8, 1, 23, 59, tzinfo=timezone.utc)
    path = app.partition_path(tmp_path, "btcusdt@aggTrade", event_time)
    assert path == (
        tmp_path
        / "binance"
        / "spot"
        / "aggTrade"
        / "symbol=BTCUSDT"
        / "date=2026-08-01"
        / "hour=23"
        / "events.ndjson"
    )


def test_build_record_preserves_unknown_source_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "SETTINGS", make_settings(tmp_path))
    received_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    payload = {"E": 1785585600000, "p": "65000", "future_field": "preserved"}
    path, line, symbol, stream_type = app.build_record(
        app.RawItem(stream="btcusdt@aggTrade", payload=payload, received_at=received_at)
    )
    assert path.name == "events.ndjson"
    assert '"future_field":"preserved"' in line
    assert line.endswith("\n")
    assert symbol == "BTCUSDT"
    assert stream_type == "aggTrade"


def test_settings_validation_rejects_bad_port(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, metrics_port=70000)
    with pytest.raises(ValueError, match="between 1 and 65535"):
        settings.validate()


def test_settings_validation_rejects_zero_write_attempts(tmp_path: Path) -> None:
    settings = app.Settings(
        ws_base="wss://example.test",
        symbols=("btcusdt",),
        streams=("aggTrade",),
        raw_root=tmp_path,
        quarantine_root=tmp_path / "quarantine",
        metrics_port=8000,
        queue_size=10,
        stale_after_seconds=60,
        reconnect_max_seconds=60,
        write_max_attempts=0,
    )
    with pytest.raises(ValueError, match="WRITE_MAX_ATTEMPTS"):
        settings.validate()


def test_observe_market_metrics_updates_aggregate_trade_gauges() -> None:
    app.observe_market_metrics("BTCUSDT", "aggTrade", {"p": "65432.10", "q": "0.125"})
    assert app.MARKET_LAST_PRICE.labels(symbol="BTCUSDT")._value.get() == pytest.approx(65432.10)
    assert app.MARKET_LAST_TRADE_QUANTITY.labels(symbol="BTCUSDT")._value.get() == pytest.approx(0.125)


def test_observe_market_metrics_updates_kline_gauges() -> None:
    payload = {"k": {"i": "1m", "c": "65430.50", "v": "12.5", "q": "817881.25"}}
    app.observe_market_metrics("BTCUSDT", "kline_1m", payload)
    assert app.MARKET_KLINE_CLOSE.labels(symbol="BTCUSDT", interval="1m")._value.get() == pytest.approx(65430.50)
    assert app.MARKET_KLINE_BASE_VOLUME.labels(symbol="BTCUSDT", interval="1m")._value.get() == pytest.approx(12.5)
    assert app.MARKET_KLINE_QUOTE_VOLUME.labels(symbol="BTCUSDT", interval="1m")._value.get() == pytest.approx(817881.25)
