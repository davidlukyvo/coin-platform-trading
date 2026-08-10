from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, Info, generate_latest

from quality import parse_sources, scan, timeframe_readiness


ROOT = Path(os.getenv("SILVER_ROOT", "/data/silver"))
SOURCES = parse_sources(os.getenv(
    "SILVER_QUALITY_SOURCES",
    "binance:spot:websocket:BTCUSDT:kline_1m;binance:spot:websocket:ETHUSDT:kline_1m;"
    "bingx:spot:websocket:BTC-USDT:kline_1min;bingx:spot:websocket:ETH-USDT:kline_1min",
))
INTERVAL = int(os.getenv("SILVER_QUALITY_INTERVAL_SECONDS", "300"))
WARNING_LAG = int(os.getenv("SILVER_QUALITY_WARNING_LAG_SECONDS", "1800"))
STALE_LAG = int(os.getenv("SILVER_QUALITY_STALE_LAG_SECONDS", "3900"))
LABELS = ("exchange", "market", "symbol")
SOURCE = Info("silver_source", "Authoritative configured Silver source", LABELS)
EXPECTED = Gauge("silver_symbol_expected", "Expected configured Silver symbol", LABELS)
EARLIEST = Gauge("silver_symbol_earliest_candle_unixtime", "Earliest closed 1m candle", LABELS)
LATEST = Gauge("silver_symbol_latest_candle_unixtime", "Latest closed 1m candle", LABELS)
LAST_CLOSE = Gauge("silver_symbol_last_close_unixtime", "Latest closed 1m candle close time", LABELS)
LAG = Gauge("silver_symbol_lag_seconds", "Seconds since latest closed 1m candle", LABELS)
ROWS = Gauge("silver_symbol_rows", "Distinct closed 1m Silver rows", (*LABELS, "window"))
EXPECTED_ROWS = Gauge("silver_symbol_expected_rows", "Expected continuous 1m rows", (*LABELS, "window"))
MISSING = Gauge("silver_symbol_missing_candles", "Missing 1m candles inside measured coverage", (*LABELS, "window"))
COMPLETENESS = Gauge("silver_symbol_completeness_ratio", "Silver 1m completeness ratio", (*LABELS, "window"))
STATUS = Gauge("silver_symbol_health_status", "Silver symbol status: 2 healthy, 1 delayed, 0 stale/gapped", LABELS)
READY = Gauge("silver_timeframe_ready", "Timeframe derivable from complete Silver 1m", (*LABELS, "timeframe", "mode"))
LAST_SCAN = Gauge("silver_quality_last_success_unixtime", "Last complete quality scan")
SCAN_DURATION = Gauge("silver_quality_scan_duration_seconds", "Quality scan duration")
ERRORS = Counter("silver_quality_errors_total", "Quality scan errors", ["exchange", "symbol", "type"])
runtime = {"healthy": False, "last_success": 0.0, "errors": 0}


def execute() -> None:
    started, failures = time.monotonic(), 0
    for source in SOURCES:
        labels = (source.exchange, source.market, source.symbol)
        SOURCE.labels(*labels).info({"feed": source.feed, "base_timeframe": "1m", "stream": source.stream})
        EXPECTED.labels(*labels).set(1)
        try:
            quality = scan(ROOT, source, time.time(), WARNING_LAG, STALE_LAG)
            EARLIEST.labels(*labels).set(quality.earliest); LATEST.labels(*labels).set(quality.latest)
            LAST_CLOSE.labels(*labels).set(quality.last_close); LAG.labels(*labels).set(quality.lag_seconds)
            STATUS.labels(*labels).set(quality.status)
            for window, rows, expected, missing, ratio in (
                ("coverage", quality.rows_total, quality.expected_total, quality.missing_total, quality.completeness_total),
                ("today", quality.rows_today, quality.expected_today, quality.missing_today, quality.completeness_today),
            ):
                ROWS.labels(*labels, window).set(rows); EXPECTED_ROWS.labels(*labels, window).set(expected)
                MISSING.labels(*labels, window).set(missing); COMPLETENESS.labels(*labels, window).set(ratio)
            for timeframe, ready in timeframe_readiness(quality).items():
                READY.labels(*labels, timeframe, "on_demand").set(ready)
        except Exception as exc:
            failures += 1; STATUS.labels(*labels).set(0)
            ERRORS.labels(source.exchange, source.symbol, type(exc).__name__).inc()
    SCAN_DURATION.set(time.monotonic() - started)
    runtime.update(healthy=failures == 0, last_success=time.time(), errors=failures)
    if failures == 0:
        LAST_SCAN.set(runtime["last_success"])


def scheduler() -> None:
    while True:
        time.sleep(INTERVAL); execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if runtime["healthy"] else "unhealthy",
                               "mode": "READ_ONLY", "sources": len(SOURCES), "errors": runtime["errors"]}).encode()
            code, kind = (200 if runtime["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute(); threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
