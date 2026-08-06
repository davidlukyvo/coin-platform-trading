from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
import websockets

logging.basicConfig(
    level=os.getenv("COLLECTOR_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("collector")

EVENTS = Counter("coin_collector_events_total", "Received market events", ["symbol", "stream"])
ERRORS = Counter("coin_collector_errors_total", "Collector errors", ["type"])
RECONNECTS = Counter("coin_collector_reconnects_total", "WebSocket reconnect attempts")
WRITTEN_BYTES = Counter("coin_collector_written_bytes_total", "RAW bytes written", ["symbol", "stream"])
QUARANTINED = Counter("coin_collector_quarantined_total", "Events moved to quarantine", ["reason"])
CONNECTED = Gauge("coin_collector_connected", "WebSocket connection state")
QUEUE_DEPTH = Gauge("coin_collector_write_queue_depth", "Pending RAW records")
CURRENT_BACKOFF = Gauge("coin_collector_reconnect_backoff_seconds", "Current reconnect delay")
LAST_EVENT_TS = Gauge("coin_collector_last_event_unixtime", "Unix time of latest received event", ["symbol", "stream"])
LAST_WRITE_TS = Gauge("coin_collector_last_write_unixtime", "Unix time of latest successful RAW write", ["symbol", "stream"])
WRITE_LATENCY = Histogram("coin_collector_write_seconds", "RAW write latency")
MARKET_LAST_PRICE = Gauge("coin_market_last_price", "Latest public trade price", ["symbol"])
MARKET_LAST_TRADE_QUANTITY = Gauge(
    "coin_market_last_trade_quantity", "Latest public trade base quantity", ["symbol"]
)
MARKET_KLINE_CLOSE = Gauge("coin_market_kline_close", "Latest public kline close", ["symbol", "interval"])
MARKET_KLINE_BASE_VOLUME = Gauge(
    "coin_market_kline_base_volume", "Current public kline base volume", ["symbol", "interval"]
)
MARKET_KLINE_QUOTE_VOLUME = Gauge(
    "coin_market_kline_quote_volume", "Current public kline quote volume", ["symbol", "interval"]
)


@dataclass(frozen=True)
class Settings:
    exchange: str
    ws_base: str
    symbols: tuple[str, ...]
    streams: tuple[str, ...]
    raw_root: Path
    quarantine_root: Path
    metrics_port: int
    queue_size: int
    stale_after_seconds: int
    reconnect_max_seconds: int
    write_max_attempts: int

    @classmethod
    def from_env(cls) -> "Settings":
        exchange = os.getenv("EXCHANGE", "binance").strip().lower()
        symbols = tuple(s.strip() for s in os.getenv("MARKET_SYMBOLS", "btcusdt,ethusdt").split(",") if s.strip())
        streams = tuple(s.strip() for s in os.getenv("MARKET_STREAMS", "aggTrade,kline_1m").split(",") if s.strip())
        settings = cls(
            exchange=exchange,
            ws_base=os.getenv(
                "BINGX_WS_BASE" if exchange == "bingx" else "BINANCE_WS_BASE",
                "wss://open-api-ws.bingx.com/market" if exchange == "bingx" else "wss://stream.binance.com:9443",
            ).rstrip("/"),
            symbols=symbols,
            streams=streams,
            raw_root=Path(os.getenv("RAW_DATA_ROOT", "/data/bronze")),
            quarantine_root=Path(os.getenv("QUARANTINE_DATA_ROOT", "/data/quarantine")),
            metrics_port=int(os.getenv("COLLECTOR_METRICS_PORT", "8000")),
            queue_size=int(os.getenv("COLLECTOR_QUEUE_SIZE", "10000")),
            stale_after_seconds=int(os.getenv("COLLECTOR_STALE_AFTER_SECONDS", "60")),
            reconnect_max_seconds=int(os.getenv("COLLECTOR_RECONNECT_MAX_SECONDS", "60")),
            write_max_attempts=int(os.getenv("COLLECTOR_WRITE_MAX_ATTEMPTS", "5")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.exchange not in {"binance", "bingx"}:
            raise ValueError("EXCHANGE must be binance or bingx")
        if not self.ws_base.startswith(("ws://", "wss://")):
            raise ValueError("WebSocket base must start with ws:// or wss://")
        if not self.symbols:
            raise ValueError("MARKET_SYMBOLS cannot be empty")
        if not self.streams:
            raise ValueError("MARKET_STREAMS cannot be empty")
        if not 1 <= self.metrics_port <= 65535:
            raise ValueError("COLLECTOR_METRICS_PORT must be between 1 and 65535")
        if self.queue_size < 1:
            raise ValueError("COLLECTOR_QUEUE_SIZE must be positive")
        if self.stale_after_seconds < 1 or self.reconnect_max_seconds < 1:
            raise ValueError("stale and reconnect values must be positive")
        if self.write_max_attempts < 1:
            raise ValueError("COLLECTOR_WRITE_MAX_ATTEMPTS must be positive")
        stream_names(self.exchange, self.symbols, self.streams)


@dataclass
class RuntimeState:
    connected: bool = False
    connection_ready: bool = False
    last_event_at: datetime | None = None
    last_write_at: datetime | None = None
    write_failed: bool = False
    fatal_error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RawItem:
    stream: str
    payload: dict[str, Any]
    received_at: datetime


shutdown_event = asyncio.Event()
state = RuntimeState()


def canonical_symbol(exchange: str, symbol: str) -> str:
    compact = symbol.replace("-", "").upper()
    if exchange == "bingx":
        if not compact.endswith("USDT") or len(compact) <= 4:
            raise ValueError(f"Unsupported BingX symbol: {symbol}")
        return f"{compact[:-4]}-USDT"
    return compact.lower()


def stream_names(exchange: str, symbols: tuple[str, ...], streams: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        normalized_symbol = canonical_symbol(exchange, symbol)
        for stream in streams:
            if exchange == "bingx" and stream in {"trade", "aggTrade"}:
                result.append(f"{normalized_symbol}@trade")
            elif exchange == "bingx" and stream in {"kline_1m", "kline_1min"}:
                result.append(f"{normalized_symbol}@kline_1min")
            elif exchange == "binance" and stream == "aggTrade":
                result.append(f"{normalized_symbol}@aggTrade")
            elif exchange == "binance" and stream.startswith("kline_") and stream.removeprefix("kline_"):
                result.append(f"{normalized_symbol}@kline_{stream.removeprefix('kline_')}")
            else:
                raise ValueError(f"Unsupported stream: {stream}")
    return result


def parse_expected_stream(stream: str, allowed_streams: frozenset[str]) -> tuple[str, str]:
    if stream not in allowed_streams:
        raise ValueError(f"unexpected stream: {stream}")
    symbol, stream_type = stream.split("@", 1)
    if not symbol or not stream_type:
        raise ValueError(f"malformed stream: {stream}")
    return symbol.replace("-", "").upper(), stream_type


def observe_market_metrics(symbol: str, stream_type: str, payload: dict[str, Any]) -> None:
    """Update non-authoritative display metrics without changing the RAW record."""
    if stream_type in {"aggTrade", "trade"}:
        MARKET_LAST_PRICE.labels(symbol=symbol).set(float(payload["p"]))
        MARKET_LAST_TRADE_QUANTITY.labels(symbol=symbol).set(float(payload["q"]))
        return
    if stream_type.startswith("kline_"):
        kline = payload.get("k") or payload["K"]
        interval = "1m" if str(kline["i"]) == "1min" else str(kline["i"])
        MARKET_KLINE_CLOSE.labels(symbol=symbol, interval=interval).set(float(kline["c"]))
        MARKET_KLINE_BASE_VOLUME.labels(symbol=symbol, interval=interval).set(float(kline["v"]))
        MARKET_KLINE_QUOTE_VOLUME.labels(symbol=symbol, interval=interval).set(float(kline["q"]))


def partition_path(raw_root: Path, exchange: str, stream: str, event_time: datetime) -> Path:
    symbol, stream_type = stream.split("@", 1)
    return (
        raw_root
        / exchange
        / "spot"
        / stream_type
        / f"symbol={symbol.upper()}"
        / f"date={event_time:%Y-%m-%d}"
        / f"hour={event_time:%H}"
        / "events.ndjson"
    )


def build_record(item: RawItem) -> tuple[Path, str, str, str]:
    source_ms = item.payload.get("E") or item.payload.get("T")
    try:
        event_time = datetime.fromtimestamp(int(source_ms) / 1000, tz=timezone.utc) if source_ms else item.received_at
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError("invalid source timestamp") from exc
    record = {
        "exchange": SETTINGS.exchange,
        "market_type": "spot",
        "stream": item.stream,
        "exchange_event_time": event_time.isoformat(),
        "collector_receive_time": item.received_at.isoformat(),
        "schema_version": 1,
        "raw_payload": item.payload,
    }
    symbol, stream_type = item.stream.split("@", 1)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    return partition_path(SETTINGS.raw_root, SETTINGS.exchange, item.stream, event_time), line, symbol.replace("-", "").upper(), stream_type


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def quarantine_line(item: RawItem, reason: str, error: str) -> None:
    now = datetime.now(timezone.utc)
    path = SETTINGS.quarantine_root / f"date={now:%Y-%m-%d}" / f"hour={now:%H}" / "failed-events.ndjson"
    record = {
        "quarantined_at": now.isoformat(),
        "reason": reason,
        "error": error,
        "stream": item.stream,
        "collector_receive_time": item.received_at.isoformat(),
        "raw_payload": item.payload,
    }
    append_line(path, json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


async def writer(queue: asyncio.Queue[RawItem]) -> None:
    while not shutdown_event.is_set() or not queue.empty():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        try:
            path, line, symbol, stream_type = build_record(item)
            last_error: Exception | None = None
            for attempt in range(1, SETTINGS.write_max_attempts + 1):
                try:
                    started = time.monotonic()
                    await asyncio.to_thread(append_line, path, line)
                    WRITE_LATENCY.observe(time.monotonic() - started)
                    WRITTEN_BYTES.labels(symbol=symbol, stream=stream_type).inc(len(line.encode("utf-8")))
                    LAST_WRITE_TS.labels(symbol=symbol, stream=stream_type).set(time.time())
                    state.last_write_at = datetime.now(timezone.utc)
                    state.write_failed = False
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    ERRORS.labels(type="write_retry").inc()
                    state.write_failed = True
                    logger.exception("RAW write attempt %s/%s failed", attempt, SETTINGS.write_max_attempts)
                    if attempt < SETTINGS.write_max_attempts:
                        await asyncio.sleep(min(2 ** (attempt - 1), 10))
            if last_error is not None:
                try:
                    await asyncio.to_thread(quarantine_line, item, "raw_write_failed", repr(last_error))
                    QUARANTINED.labels(reason="raw_write_failed").inc()
                    ERRORS.labels(type="write_quarantined").inc()
                    logger.error("RAW event moved to quarantine after exhausted retries")
                except OSError as quarantine_error:
                    state.fatal_error = f"RAW and quarantine writes failed: {quarantine_error!r}"
                    ERRORS.labels(type="quarantine_write").inc()
                    logger.critical(state.fatal_error, exc_info=True)
                    shutdown_event.set()
        except (ValueError, TypeError) as exc:
            state.write_failed = True
            ERRORS.labels(type="record_validation").inc()
            try:
                await asyncio.to_thread(quarantine_line, item, "record_validation_failed", repr(exc))
                QUARANTINED.labels(reason="record_validation_failed").inc()
            except OSError as quarantine_error:
                state.fatal_error = f"Validation quarantine write failed: {quarantine_error!r}"
                logger.critical(state.fatal_error, exc_info=True)
                shutdown_event.set()
        finally:
            queue.task_done()
            QUEUE_DEPTH.set(queue.qsize())


async def collect(queue: asyncio.Queue[RawItem]) -> None:
    streams = stream_names(SETTINGS.exchange, SETTINGS.symbols, SETTINGS.streams)
    allowed_streams = frozenset(streams)
    url = f"{SETTINGS.ws_base}/stream?streams={'/'.join(streams)}" if SETTINGS.exchange == "binance" else SETTINGS.ws_base
    backoff = 1.0
    while not shutdown_event.is_set():
        state.connected = False
        state.connection_ready = False
        CONNECTED.set(0)
        try:
            logger.info("Connecting to %s streams: %s", SETTINGS.exchange, ", ".join(streams))
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_queue=4096,
                open_timeout=20,
            ) as ws:
                state.connected = True
                CONNECTED.set(1)
                if SETTINGS.exchange == "bingx":
                    for index, stream in enumerate(streams):
                        await ws.send(json.dumps({"id": f"trading-bamboo-{index}", "dataType": stream}))
                async for message in ws:
                    if shutdown_event.is_set():
                        break
                    received_at = datetime.now(timezone.utc)
                    try:
                        if isinstance(message, bytes):
                            message = gzip.decompress(message).decode("utf-8")
                        if SETTINGS.exchange == "bingx" and "ping" in message.lower():
                            await ws.send("Pong")
                            continue
                        envelope = json.loads(message)
                        if SETTINGS.exchange == "bingx" and "code" in envelope and "data" not in envelope:
                            if int(envelope["code"]) != 0:
                                raise ValueError(f"BingX subscription failed: {envelope}")
                            continue
                        stream = envelope["dataType" if SETTINGS.exchange == "bingx" else "stream"]
                        payload = envelope["data"]
                        if not isinstance(stream, str) or not isinstance(payload, dict):
                            raise ValueError("invalid market-data envelope")
                        symbol, stream_type = parse_expected_stream(stream, allowed_streams)
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        ERRORS.labels(type="decode_or_protocol").inc()
                        logger.exception("Invalid or unexpected WebSocket envelope")
                        continue

                    try:
                        queue.put_nowait(RawItem(stream=stream, payload=payload, received_at=received_at))
                    except asyncio.QueueFull:
                        ERRORS.labels(type="queue_full").inc()
                        logger.error("RAW queue full; disconnecting to avoid silent data loss")
                        raise RuntimeError("RAW queue full")

                    EVENTS.labels(symbol=symbol, stream=stream_type).inc()
                    LAST_EVENT_TS.labels(symbol=symbol, stream=stream_type).set(received_at.timestamp())
                    try:
                        observe_market_metrics(symbol, stream_type, payload)
                    except (KeyError, TypeError, ValueError):
                        ERRORS.labels(type="derived_market_metric").inc()
                        logger.warning("Unable to derive market display metric for %s", stream, exc_info=True)
                    QUEUE_DEPTH.set(queue.qsize())
                    state.last_event_at = received_at
                    if not state.connection_ready:
                        state.connection_ready = True
                        backoff = 1.0
                        CURRENT_BACKOFF.set(0)
        except asyncio.CancelledError:
            raise
        except (OSError, websockets.WebSocketException, RuntimeError):
            ERRORS.labels(type="websocket").inc()
            RECONNECTS.inc()
            delay = min(backoff, float(SETTINGS.reconnect_max_seconds))
            jittered = random.uniform(delay * 0.8, delay * 1.2)
            CURRENT_BACKOFF.set(jittered)
            logger.exception("Collector connection failed; reconnecting in %.1f seconds", jittered)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=jittered)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, float(SETTINGS.reconnect_max_seconds))
        finally:
            state.connected = False
            state.connection_ready = False
            CONNECTED.set(0)
    CURRENT_BACKOFF.set(0)


async def healthz(_: web.Request) -> web.Response:
    now = datetime.now(timezone.utc)
    stale = state.last_event_at is None or (now - state.last_event_at).total_seconds() > SETTINGS.stale_after_seconds
    healthy = state.connected and state.connection_ready and not stale and not state.write_failed and state.fatal_error is None
    return web.json_response(
        {
            "status": "healthy" if healthy else "unhealthy",
            "connected": state.connected,
            "connection_ready": state.connection_ready,
            "write_failed": state.write_failed,
            "fatal_error": state.fatal_error,
            "last_event_at": state.last_event_at.isoformat() if state.last_event_at else None,
            "last_write_at": state.last_write_at.isoformat() if state.last_write_at else None,
        },
        status=200 if healthy else 503,
    )


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


async def serve_http() -> None:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SETTINGS.metrics_port)
    await site.start()
    logger.info("Health and metrics listening on port %s", SETTINGS.metrics_port)
    await shutdown_event.wait()
    await runner.cleanup()


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: shutdown_event.set())

    for root in (SETTINGS.raw_root, SETTINGS.quarantine_root):
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(f"Data root is not writable: {root}") from exc

    queue: asyncio.Queue[RawItem] = asyncio.Queue(maxsize=SETTINGS.queue_size)
    collector_task = asyncio.create_task(collect(queue), name="collector")
    writer_task = asyncio.create_task(writer(queue), name="writer")
    http_task = asyncio.create_task(serve_http(), name="http")

    await shutdown_event.wait()
    collector_task.cancel()
    await asyncio.gather(collector_task, return_exceptions=True)
    try:
        await asyncio.wait_for(queue.join(), timeout=30)
    except asyncio.TimeoutError:
        logger.error("Timed out draining RAW queue during shutdown")
    writer_task.cancel()
    http_task.cancel()
    await asyncio.gather(writer_task, http_task, return_exceptions=True)


SETTINGS = Settings.from_env()

if __name__ == "__main__":
    asyncio.run(main())
