import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
import websockets

LOG_LEVEL = os.getenv("COLLECTOR_LOG_LEVEL", "INFO").upper()
WS_BASE = os.getenv("BINANCE_WS_BASE", "wss://stream.binance.com:9443").rstrip("/")
SYMBOLS = [s.strip().lower() for s in os.getenv("MARKET_SYMBOLS", "btcusdt,ethusdt").split(",") if s.strip()]
STREAMS = [s.strip() for s in os.getenv("MARKET_STREAMS", "aggTrade,kline_1m").split(",") if s.strip()]
RAW_ROOT = Path(os.getenv("RAW_DATA_ROOT", "/data/bronze"))
METRICS_PORT = int(os.getenv("COLLECTOR_METRICS_PORT", "8000"))

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collector")

EVENTS = Counter("coin_collector_events_total", "Received market events", ["stream"])
ERRORS = Counter("coin_collector_errors_total", "Collector errors", ["type"])
CONNECTED = Gauge("coin_collector_connected", "WebSocket connection state")
LAST_EVENT_TS = Gauge("coin_collector_last_event_unixtime", "Unix time of latest event")

shutdown_event = asyncio.Event()
last_event_at: datetime | None = None


def stream_names() -> list[str]:
    result: list[str] = []
    for symbol in SYMBOLS:
        for stream in STREAMS:
            if stream == "aggTrade":
                result.append(f"{symbol}@aggTrade")
            elif stream.startswith("kline_"):
                result.append(f"{symbol}@kline_{stream.removeprefix('kline_')}")
            else:
                raise ValueError(f"Unsupported stream: {stream}")
    return result


def partition_path(stream: str, event_time: datetime) -> Path:
    symbol, stream_type = stream.split("@", 1)
    return (
        RAW_ROOT
        / "binance"
        / "spot"
        / stream_type
        / f"symbol={symbol.upper()}"
        / f"date={event_time:%Y-%m-%d}"
        / f"hour={event_time:%H}"
        / "events.ndjson"
    )


def write_raw(stream: str, payload: dict[str, Any], received_at: datetime) -> None:
    source_ms = payload.get("E") or payload.get("T")
    event_time = datetime.fromtimestamp(source_ms / 1000, tz=timezone.utc) if source_ms else received_at
    record = {
        "exchange": "binance",
        "market_type": "spot",
        "stream": stream,
        "exchange_event_time": event_time.isoformat(),
        "collector_receive_time": received_at.isoformat(),
        "schema_version": 1,
        "raw_payload": payload,
    }
    path = partition_path(stream, event_time)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


async def collect() -> None:
    global last_event_at
    streams = stream_names()
    url = f"{WS_BASE}/stream?streams={'/'.join(streams)}"
    backoff = 1
    while not shutdown_event.is_set():
        try:
            logger.info("Connecting to Binance streams: %s", ", ".join(streams))
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=4096) as ws:
                CONNECTED.set(1)
                backoff = 1
                async for message in ws:
                    received_at = datetime.now(timezone.utc)
                    envelope = json.loads(message)
                    stream = envelope["stream"]
                    payload = envelope["data"]
                    write_raw(stream, payload, received_at)
                    EVENTS.labels(stream=stream.split("@", 1)[1]).inc()
                    last_event_at = received_at
                    LAST_EVENT_TS.set(received_at.timestamp())
                    if shutdown_event.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            CONNECTED.set(0)
            ERRORS.labels(type="websocket_or_write").inc()
            logger.exception("Collector loop failed; reconnecting in %s seconds", backoff)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
    CONNECTED.set(0)


async def healthz(_: web.Request) -> web.Response:
    stale = last_event_at is None or (datetime.now(timezone.utc) - last_event_at).total_seconds() > 60
    status = 503 if stale or CONNECTED._value.get() != 1 else 200
    return web.json_response(
        {
            "status": "unhealthy" if status != 200 else "healthy",
            "connected": CONNECTED._value.get() == 1,
            "last_event_at": last_event_at.isoformat() if last_event_at else None,
        },
        status=status,
    )


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


async def serve_http() -> None:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", METRICS_PORT)
    await site.start()
    logger.info("Health and metrics listening on port %s", METRICS_PORT)
    await shutdown_event.wait()
    await runner.cleanup()


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)
    await asyncio.gather(collect(), serve_http())


if __name__ == "__main__":
    asyncio.run(main())
