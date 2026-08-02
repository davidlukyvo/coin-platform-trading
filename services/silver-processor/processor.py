from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

UTC = timezone.utc
DECIMAL = pa.decimal128(38, 18)

AGG_SCHEMA = pa.schema([
    ("exchange", pa.string()), ("market_type", pa.string()), ("symbol", pa.string()),
    ("stream", pa.string()), ("schema_version", pa.int16()),
    ("exchange_event_time", pa.timestamp("ms", tz="UTC")),
    ("collector_receive_time", pa.timestamp("ms", tz="UTC")),
    ("aggregate_trade_id", pa.int64()), ("price", DECIMAL), ("quantity", DECIMAL),
    ("first_trade_id", pa.int64()), ("last_trade_id", pa.int64()),
    ("trade_time", pa.timestamp("ms", tz="UTC")), ("buyer_is_maker", pa.bool_()),
])

KLINE_SCHEMA = pa.schema([
    ("exchange", pa.string()), ("market_type", pa.string()), ("symbol", pa.string()),
    ("interval", pa.string()), ("schema_version", pa.int16()),
    ("exchange_event_time", pa.timestamp("ms", tz="UTC")),
    ("collector_receive_time", pa.timestamp("ms", tz="UTC")),
    ("open_time", pa.timestamp("ms", tz="UTC")), ("close_time", pa.timestamp("ms", tz="UTC")),
    ("open", DECIMAL), ("high", DECIMAL), ("low", DECIMAL), ("close", DECIMAL),
    ("volume", DECIMAL), ("quote_volume", DECIMAL), ("trade_count", pa.int64()),
    ("taker_buy_base_volume", DECIMAL), ("taker_buy_quote_volume", DECIMAL),
    ("is_closed", pa.bool_()),
])


def utc_datetime(value: Any, *, milliseconds: bool = False) -> datetime:
    if milliseconds:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def positive_decimal(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise ValueError(f"invalid {name}")
    return result


def normalize_agg(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw = record["raw_payload"]
    symbol = str(raw["s"]).upper()
    trade_id = int(raw["a"])
    row = {
        "exchange": str(record["exchange"]), "market_type": str(record["market_type"]),
        "symbol": symbol, "stream": "aggTrade", "schema_version": int(record["schema_version"]),
        "exchange_event_time": utc_datetime(record["exchange_event_time"]),
        "collector_receive_time": utc_datetime(record["collector_receive_time"]),
        "aggregate_trade_id": trade_id, "price": positive_decimal(raw["p"], "price"),
        "quantity": positive_decimal(raw["q"], "quantity"), "first_trade_id": int(raw["f"]),
        "last_trade_id": int(raw["l"]), "trade_time": utc_datetime(raw["T"], milliseconds=True),
        "buyer_is_maker": bool(raw["m"]),
    }
    return f"{symbol}:{trade_id}", row


def normalize_kline(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw = record["raw_payload"]
    kline = raw["k"]
    symbol, interval = str(kline["s"]).upper(), str(kline["i"])
    if interval != "1m":
        raise ValueError("unsupported kline interval")
    open_, high, low, close = (positive_decimal(kline[key], key) for key in ("o", "h", "l", "c"))
    if high < max(open_, close, low) or low > min(open_, close, high):
        raise ValueError("invalid OHLC relationship")
    open_time = utc_datetime(kline["t"], milliseconds=True)
    close_time = utc_datetime(kline["T"], milliseconds=True)
    if close_time < open_time:
        raise ValueError("close_time before open_time")
    trade_count = int(kline["n"])
    if trade_count < 0:
        raise ValueError("negative trade_count")
    row = {
        "exchange": str(record["exchange"]), "market_type": str(record["market_type"]),
        "symbol": symbol, "interval": interval, "schema_version": int(record["schema_version"]),
        "exchange_event_time": utc_datetime(record["exchange_event_time"]),
        "collector_receive_time": utc_datetime(record["collector_receive_time"]),
        "open_time": open_time, "close_time": close_time, "open": open_, "high": high,
        "low": low, "close": close, "volume": positive_decimal(kline["v"], "volume", allow_zero=True),
        "quote_volume": positive_decimal(kline["q"], "quote_volume", allow_zero=True),
        "trade_count": trade_count,
        "taker_buy_base_volume": positive_decimal(kline["V"], "taker_buy_base_volume", allow_zero=True),
        "taker_buy_quote_volume": positive_decimal(kline["Q"], "taker_buy_quote_volume", allow_zero=True),
        "is_closed": bool(kline["x"]),
    }
    return f"{symbol}:{interval}:{int(open_time.timestamp() * 1000)}", row


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_parquet(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, temp, compression="zstd", version="2.6")
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Checkpoint:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS checkpoints(path TEXT PRIMARY KEY, offset INTEGER NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS seen(stream TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(stream,key))")
        self.connection.commit()

    def offset(self, path: str) -> int:
        row = self.connection.execute("SELECT offset FROM checkpoints WHERE path=?", (path,)).fetchone()
        return int(row[0]) if row else 0

    def unseen(self, stream: str, keys: Iterable[str]) -> set[str]:
        result = set(keys)
        for key in tuple(result):
            if self.connection.execute("SELECT 1 FROM seen WHERE stream=? AND key=?", (stream, key)).fetchone():
                result.discard(key)
        return result

    def commit(self, path: str, offset: int, stream: str, keys: Iterable[str]) -> None:
        with self.connection:
            self.connection.executemany("INSERT OR IGNORE INTO seen(stream,key) VALUES(?,?)", ((stream, key) for key in keys))
            self.connection.execute(
                "INSERT INTO checkpoints(path,offset) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET offset=excluded.offset",
                (path, offset),
            )


@dataclass
class BatchResult:
    input_rows: int = 0
    valid_rows: int = 0
    written_rows: int = 0
    rejected_rows: int = 0
    duplicates: int = 0
    files_written: int = 0
    offset: int = 0
    max_event_time: datetime | None = None


class SilverProcessor:
    def __init__(self, bronze: Path, silver: Path, quarantine: Path, reports: Path, checkpoint: Checkpoint,
                 max_bytes: int = 16 * 1024 * 1024, min_batch_bytes: int = 4 * 1024 * 1024):
        self.bronze, self.silver, self.quarantine, self.reports = bronze, silver, quarantine, reports
        self.checkpoint, self.max_bytes, self.min_batch_bytes = checkpoint, max_bytes, min_batch_bytes

    def files(self, start_date: str | None = None, end_date: str | None = None) -> list[Path]:
        files = sorted(self.bronze.glob("binance/spot/*/symbol=*/date=*/hour=*/events.ndjson"))
        if start_date or end_date:
            files = [path for path in files if (not start_date or path.parts[-3].split("=", 1)[1] >= start_date)
                     and (not end_date or path.parts[-3].split("=", 1)[1] <= end_date)]
        return files

    def process_file(self, path: Path, *, dry_run: bool = False, use_checkpoint: bool = True) -> BatchResult:
        relative = path.relative_to(self.bronze).as_posix()
        stream = path.parts[-5]
        start = self.checkpoint.offset(relative) if use_checkpoint else 0
        result = BatchResult(offset=start)
        partition_hour = f"{path.parts[-3].split('=', 1)[1]}T{path.parts[-2].split('=', 1)[1]}"
        current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        # Let the active Bronze file accumulate before producing another part. A
        # sealed hourly partition is always drained, including a short final tail.
        if use_checkpoint and partition_hour == current_hour and path.stat().st_size - start < self.min_batch_bytes:
            return result
        records: list[tuple[str, dict[str, Any]]] = []
        rejected: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            handle.seek(start)
            while handle.tell() - start < self.max_bytes:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(line_start)
                    break
                result.input_rows += 1
                try:
                    obj = json.loads(line)
                    normalized = normalize_agg(obj) if stream == "aggTrade" else normalize_kline(obj)
                    records.append(normalized)
                except Exception as exc:
                    result.rejected_rows += 1
                    rejected.append({"source": relative, "offset": line_start, "error": type(exc).__name__, "reason": str(exc)[:300]})
            result.offset = handle.tell()
        if result.offset == start:
            return result

        latest: dict[str, dict[str, Any]] = {}
        for key, row in records:
            if key in latest:
                result.duplicates += 1
                if stream == "kline_1m" and row["exchange_event_time"] >= latest[key]["exchange_event_time"]:
                    latest[key] = row
            else:
                latest[key] = row
        if stream == "kline_1m":
            latest = {key: row for key, row in latest.items() if row["is_closed"]}
        result.valid_rows = len(latest)
        unseen = self.checkpoint.unseen(stream, latest) if use_checkpoint else set(latest)
        rows = [latest[key] for key in sorted(unseen)]
        result.duplicates += len(latest) - len(rows)
        result.written_rows = len(rows)
        event_times = [row["exchange_event_time"] for row in rows]
        result.max_event_time = max(event_times) if event_times else None

        symbol = path.parts[-4].split("=", 1)[1]
        date = path.parts[-3].split("=", 1)[1]
        digest = hashlib.sha256(f"{relative}:{start}:{result.offset}".encode()).hexdigest()[:16]
        output = self.silver / "binance" / "spot" / stream / f"symbol={symbol}" / f"date={date}" / f"part-{digest}.parquet"
        quarantine = self.quarantine / f"date={date}" / f"rejected-{digest}.ndjson"
        report = self.reports / f"date={date}" / f"batch-{digest}.json"
        started = time.monotonic()
        if not dry_run:
            if rows:
                atomic_parquet(output, rows, AGG_SCHEMA if stream == "aggTrade" else KLINE_SCHEMA)
                result.files_written = 1
            if rejected:
                atomic_bytes(quarantine, b"".join((json.dumps(item, separators=(",", ":")) + "\n").encode() for item in rejected))
            summary = {
                "source": relative, "start_offset": start, "end_offset": result.offset,
                "input_rows": result.input_rows, "valid_rows": result.valid_rows,
                "written_rows": result.written_rows, "rejected_rows": result.rejected_rows,
                "duplicates": result.duplicates,
                "min_event_time": min(event_times).isoformat() if event_times else None,
                "max_event_time": max(event_times).isoformat() if event_times else None,
                "duration_seconds": time.monotonic() - started,
            }
            atomic_bytes(report, (json.dumps(summary, separators=(",", ":")) + "\n").encode())
            if use_checkpoint:
                self.checkpoint.commit(relative, result.offset, stream, unseen)
        return result

    def run_once(self, *, dry_run: bool = False, use_checkpoint: bool = True,
                 start_date: str | None = None, end_date: str | None = None) -> list[BatchResult]:
        return [result for path in self.files(start_date, end_date)
                if (result := self.process_file(path, dry_run=dry_run, use_checkpoint=use_checkpoint)).input_rows]
