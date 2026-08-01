from datetime import datetime, timezone
from pathlib import Path

import pytest

import app


def test_stream_names_builds_supported_streams() -> None:
    assert app.stream_names(("btcusdt",), ("aggTrade", "kline_1m")) == [
        "btcusdt@aggTrade",
        "btcusdt@kline_1m",
    ]


def test_stream_names_rejects_unsupported_stream() -> None:
    with pytest.raises(ValueError, match="Unsupported stream"):
        app.stream_names(("btcusdt",), ("depth",))


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
    monkeypatch.setattr(app, "SETTINGS", app.Settings(
        ws_base="wss://example.test",
        symbols=("btcusdt",),
        streams=("aggTrade",),
        raw_root=tmp_path,
        metrics_port=8000,
        queue_size=10,
        stale_after_seconds=60,
        reconnect_max_seconds=60,
    ))
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


def test_settings_validation_rejects_bad_port() -> None:
    settings = app.Settings(
        ws_base="wss://example.test",
        symbols=("btcusdt",),
        streams=("aggTrade",),
        raw_root=Path("/tmp/raw"),
        metrics_port=70000,
        queue_size=1,
        stale_after_seconds=60,
        reconnect_max_seconds=60,
    )
    with pytest.raises(ValueError, match="between 1 and 65535"):
        settings.validate()
