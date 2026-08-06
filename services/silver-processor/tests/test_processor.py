import json
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from processor import Checkpoint, SilverProcessor, atomic_bytes, normalize_agg, normalize_kline


def outer(payload, event="2026-08-02T01:00:00+00:00"):
    return {"exchange":"binance","market_type":"spot","stream":"x","schema_version":1,
            "exchange_event_time":event,"collector_receive_time":event,"raw_payload":payload}


def agg(trade_id=1, price="65000.123456789012345678", quantity="0.000000010000000001"):
    return outer({"s":"BTCUSDT","a":trade_id,"p":price,"q":quantity,"f":trade_id,"l":trade_id,
                  "T":1785632400000,"m":False})


def kline(closed=True, event_ms=1785632459000, open_time=1785632400000):
    record = outer({"s":"BTCUSDT","k":{"s":"BTCUSDT","i":"1m","t":open_time,"T":open_time+59999,
        "o":"65000.1","h":"65100.2","l":"64900.3","c":"65050.4","v":"12.5","q":"812500.0",
        "n":42,"V":"6.0","Q":"390000.0","x":closed}},
        event="2026-08-02T01:00:59+00:00")
    record["raw_payload"]["E"] = event_ms
    return record


def source(root: Path, stream: str) -> Path:
    path = root / "binance" / "spot" / stream / "symbol=BTCUSDT" / "date=2026-08-02" / "hour=01" / "events.ndjson"
    path.parent.mkdir(parents=True)
    return path


def bingx_outer(payload, event="2026-08-02T01:02:00+00:00"):
    return {"exchange":"bingx","market_type":"spot","stream":"x","schema_version":1,
            "exchange_event_time":event,"collector_receive_time":event,"raw_payload":payload}


def bingx_source(root: Path, stream: str) -> Path:
    path = root / "bingx" / "spot" / stream / "symbol=BTC-USDT" / "date=2026-08-02" / "hour=01" / "events.ndjson"
    path.parent.mkdir(parents=True)
    return path


def make_processor(tmp_path: Path):
    bronze = tmp_path / "bronze"
    checkpoint = Checkpoint(tmp_path / "checkpoints" / "state.db")
    return SilverProcessor(bronze, tmp_path/"silver", tmp_path/"silver-quarantine", tmp_path/"reports", checkpoint), bronze


def append(path: Path, *records, trailing=b""):
    with path.open("ab") as handle:
        for record in records:
            handle.write((json.dumps(record)+"\n").encode())
        handle.write(trailing)


def test_parse_agg_decimal_precision_and_utc():
    key, row = normalize_agg(agg())
    assert key == "BTCUSDT:1"
    assert row["price"] == Decimal("65000.123456789012345678")
    assert row["quantity"] == Decimal("0.000000010000000001")
    assert row["trade_time"].tzinfo == timezone.utc


def test_parse_kline_and_closed_state():
    key, row = normalize_kline(kline(True))
    assert key.startswith("BTCUSDT:1m:")
    assert row["is_closed"] is True
    assert row["high"] >= max(row["open"], row["close"], row["low"])


def test_parse_bingx_trade_maps_to_common_schema():
    record = bingx_outer({"E":1785632520000,"T":1785632520000,"e":"trade","p":"65010.5",
                          "q":"0.02","s":"BTC-USDT","t":"33685717","m":True})
    key, row = normalize_agg(record)
    assert key == "BTC-USDT:33685717"
    assert row["exchange"] == "bingx"
    assert row["stream"] == "trade"
    assert row["first_trade_id"] == row["last_trade_id"] == 33685717


def test_bingx_pipeline_is_partitioned_and_checkpoint_isolated(tmp_path):
    processor, bronze = make_processor(tmp_path)
    trade = bingx_outer({"E":1785632520000,"T":1785632520000,"e":"trade","p":"65010.5",
                         "q":"0.02","s":"BTC-USDT","t":"33685717","m":True})
    path = bingx_source(bronze, "trade")
    append(path, trade)
    result = processor.run_once()[0]
    assert result.written_rows == 1
    output = next((tmp_path / "silver" / "bingx" / "spot" / "trade").rglob("*.parquet"))
    table = pq.ParquetFile(output).read()
    assert table.column("exchange").to_pylist() == ["bingx"]
    assert processor.checkpoint.unseen("bingx:trade", ["BTC-USDT:33685717"]) == set()


@pytest.mark.parametrize("record", [agg(price="0"), agg(quantity="bad")])
def test_invalid_schema(record):
    with pytest.raises((ValueError, KeyError)):
        normalize_agg(record)


def test_malformed_json_is_quarantined(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "aggTrade")
    append(path, agg(), trailing=b"{bad json}\n")
    result = processor.run_once()[0]
    assert result.rejected_rows == 1
    assert len(list((tmp_path/"silver-quarantine").rglob("*.ndjson"))) == 1


def test_partial_trailing_line_is_not_checkpointed(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "aggTrade")
    complete = (json.dumps(agg())+"\n").encode()
    path.write_bytes(complete + b'{"partial":')
    processor.run_once()
    relative = path.relative_to(bronze).as_posix()
    assert processor.checkpoint.offset(relative) == len(complete)
    append(path, trailing=b"true}\n")
    result = processor.run_once()[0]
    assert result.rejected_rows == 1


def test_duplicate_and_rerun_idempotency(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "aggTrade")
    append(path, agg(1), agg(1), agg(2))
    result = processor.run_once()[0]
    assert (result.written_rows, result.duplicates) == (2, 1)
    files = list((tmp_path/"silver").rglob("*.parquet"))
    assert pq.ParquetFile(files[0]).read().num_rows == 2
    assert processor.run_once() == []
    assert len(list((tmp_path/"silver").rglob("*.parquet"))) == 1


def test_kline_updates_only_publish_final_candle(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "kline_1m")
    append(path, kline(False), kline(False, event_ms=1785632460000), kline(True, event_ms=1785632461000))
    result = processor.run_once()[0]
    assert result.written_rows == 1
    table = pq.ParquetFile(next((tmp_path/"silver").rglob("*.parquet"))).read()
    assert table.column("is_closed").to_pylist() == [True]


def test_open_kline_then_final_in_next_batch(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "kline_1m")
    append(path, kline(False))
    assert processor.run_once()[0].written_rows == 0
    append(path, kline(True, event_ms=1785632461000))
    assert processor.run_once()[0].written_rows == 1


def test_crash_before_checkpoint_commit_recovers_without_extra_file(tmp_path, monkeypatch):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "aggTrade")
    append(path, agg(7))
    original = processor.checkpoint.commit
    monkeypatch.setattr(processor.checkpoint, "commit", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        processor.run_once()
    assert len(list((tmp_path/"silver").rglob("*.parquet"))) == 1
    monkeypatch.setattr(processor.checkpoint, "commit", original)
    assert processor.run_once()[0].written_rows == 1
    assert len(list((tmp_path/"silver").rglob("*.parquet"))) == 1


def test_atomic_write_and_duckdb_validation(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "aggTrade")
    append(path, agg(1), agg(2))
    processor.run_once()
    parquet = str(next((tmp_path/"silver").rglob("*.parquet")))
    row = duckdb.connect().execute("select count(*), count(distinct aggregate_trade_id), min(price) from read_parquet(?)", [parquet]).fetchone()
    assert row[0] == row[1] == 2
    assert not list((tmp_path/"silver").rglob("*.tmp"))


def test_empty_file(tmp_path):
    processor, bronze = make_processor(tmp_path)
    source(bronze, "aggTrade").touch()
    assert processor.run_once() == []


def test_dry_run_does_not_write_or_advance_checkpoint(tmp_path):
    processor, bronze = make_processor(tmp_path)
    path = source(bronze, "aggTrade")
    append(path, agg(99))
    result = processor.run_once(dry_run=True)[0]
    assert result.written_rows == 1
    assert processor.checkpoint.offset(path.relative_to(bronze).as_posix()) == 0
    assert not list((tmp_path / "silver").rglob("*.parquet"))


def test_atomic_bytes_replaces_complete_file(tmp_path):
    path = tmp_path / "state.json"
    atomic_bytes(path, b"one")
    atomic_bytes(path, b"two")
    assert path.read_bytes() == b"two"
