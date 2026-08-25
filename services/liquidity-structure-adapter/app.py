import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest

from engine import LiquidityStructureAdapter, Thresholds


os.umask(0o077)
LAST = Gauge("liquidity_structure_last_success_unixtime", "Last successful liquidity-structure cycle")
ERRORS = Counter("liquidity_structure_errors_total", "Liquidity-structure cycle errors", ["type"])
JOURNAL = Gauge("liquidity_structure_journal_rows", "Exactly-once research observations")
READY = Gauge("liquidity_structure_symbol_ready", "Symbol research readiness", ["symbol"])
DECISION = Gauge("liquidity_structure_decision_info", "Latest shadow decision",
                 ["symbol", "decision", "direction", "sweep", "mss", "ote", "reason"])
DIVERGENCE = Gauge("liquidity_structure_divergence_info", "Cross-asset divergence evidence",
                   ["symbol", "peer", "event", "direction"])
PROCESSED = Counter("liquidity_structure_processed_bars_total", "Closed 5m bars journaled", ["symbol"])
state = {"healthy": False, "error": None, "projection": {}}
limits = Thresholds(max_market_age_seconds=int(os.getenv("LIQUIDITY_MAX_MARKET_AGE_SECONDS", "3900")))
adapter = LiquidityStructureAdapter(
    Path(os.getenv("SILVER_ROOT", "/data/silver")),
    Path(os.getenv("LIQUIDITY_OUTPUT_ROOT", "/data/liquidity-structure")),
    os.getenv("LIQUIDITY_DATA_EXCHANGE", "binance"),
    tuple(x.strip().upper() for x in os.getenv("LIQUIDITY_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if x.strip()),
    limits,
)


def execute():
    try:
        result = adapter.run_once()
        DECISION.clear(); DIVERGENCE.clear(); READY.clear()
        for item in result["scheduler"]:
            READY.labels(item["symbol"]).set(1 if item["status"] == "READY" else 0)
            if item["processedBars"]: PROCESSED.labels(item["symbol"]).inc(item["processedBars"])
        for signal in result["signals"]:
            DECISION.labels(signal["symbol"], signal["decision"], signal["direction"], signal["liquiditySweep"],
                            signal["marketStructureShift"], signal["oteStatus"], signal["authorityReason"]).set(1)
            for evidence in signal["crossAssetDivergence"]:
                DIVERGENCE.labels(signal["symbol"], evidence["peer"], evidence["event"], evidence["direction"]).set(1)
        JOURNAL.set(result["journalRows"]); LAST.set(time.time())
        state.update(healthy=True, error=None, projection=result)
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc(); state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("LIQUIDITY_INTERVAL_SECONDS", "60"))); execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/projection":
            body, code, kind = json.dumps(state["projection"]).encode(), 200, "application/json"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy", "mode": "SHADOW_ONLY",
                               "live_trading": False, "execution_actionable": False,
                               "execution_gate_passed": False, "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute(); threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8082), Handler).serve_forever()
