import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest

from adapter import HybridShadowAdapter, Thresholds


LAST = Gauge("hybrid_adapter_last_success_unixtime", "Last successful shadow evaluation")
SIGNALS = Gauge("hybrid_adapter_signals", "Signals by authority decision", ["decision"])
ERRORS = Counter("hybrid_adapter_errors_total", "Adapter cycle errors", ["type"])
state = {"healthy": False, "error": None, "last_success": 0.0}
adapter = HybridShadowAdapter(
    Path(os.getenv("SILVER_ROOT", "/data/silver")), Path(os.getenv("HYBRID_OUTPUT_ROOT", "/data/hybrid-signals")),
    os.getenv("HYBRID_DATA_EXCHANGE", "binance"),
    tuple(item.strip().upper() for item in os.getenv("HYBRID_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if item.strip()),
    Thresholds(max_market_age_seconds=int(os.getenv("HYBRID_MAX_MARKET_AGE_SECONDS", "3900")),
               min_relative_volume=float(os.getenv("HYBRID_MIN_RELATIVE_VOLUME", "1.0")),
               min_rr=float(os.getenv("HYBRID_MIN_RR", "2.0"))),
)


def execute():
    try:
        result = adapter.run_once()
        counts = {name: 0 for name in ("ALLOW", "WAIT", "REJECT")}
        for signal in result["signals"]: counts[signal["authorityDecision"]] += 1
        for decision, count in counts.items(): SIGNALS.labels(decision).set(count)
        LAST.set(time.time()); state.update(healthy=True, error=None, last_success=time.time())
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc(); state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("HYBRID_INTERVAL_SECONDS", "60"))); execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy", "mode": "SHADOW_ONLY",
                               "live_trading": False, "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute(); threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8060), Handler).serve_forever()
