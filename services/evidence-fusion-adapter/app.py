import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest

from engine import EvidenceFusionAdapter, FusionConfig


os.umask(0o077)
ERRORS = Counter("evidence_fusion_errors_total", "Evidence fusion cycle errors", ["type"])
JOURNAL = Gauge("evidence_fusion_journal_rows", "Exactly-once fusion observations")
READY = Gauge("evidence_fusion_symbol_ready", "Fusion readiness", ["symbol"])
DECISION = Gauge("evidence_fusion_decision_info", "Latest fusion shadow decision",
                 ["symbol", "decision", "direction", "reason"])
state = {"healthy": False, "error": None, "projection": {}}
config = FusionConfig(
    max_market_age_seconds=int(os.getenv("FUSION_MAX_MARKET_AGE_SECONDS", "3900")),
    continuity_bars=int(os.getenv("FUSION_CONTINUITY_BARS", "12")),
    liquidity_soak_status=os.getenv("LIQUIDITY_SOAK_STATUS", "FAIL").upper(),
)
adapter = EvidenceFusionAdapter(
    os.getenv("WYCKOFF_PROJECTION_URL", "http://wyckoff-shadow-adapter:8070/projection"),
    os.getenv("LIQUIDITY_PROJECTION_URL", "http://liquidity-structure-adapter:8082/projection"),
    Path(os.getenv("FUSION_OUTPUT_ROOT", "/data/evidence-fusion")),
    tuple(x.strip().upper() for x in os.getenv("FUSION_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if x.strip()),
    config,
)


def execute():
    try:
        projection = adapter.run_once()
        READY.clear(); DECISION.clear()
        for item in projection["scheduler"]:
            READY.labels(item["symbol"]).set(1 if item["status"] == "READY" else 0)
        for item in projection["signals"]:
            DECISION.labels(item["symbol"], item["decision"], item["direction"], item["reason"]).set(1)
        JOURNAL.set(projection["journalRows"])
        state.update(healthy=True, error=None, projection=projection)
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc()
        state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("FUSION_INTERVAL_SECONDS", "60")))
        execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/projection": body, code, kind = json.dumps(state["projection"]).encode(), 200, "application/json"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy",
                               "mode": "SHADOW_ONLY", "liveTrading": False,
                               "executionActionable": False, "executionGatePassed": False,
                               "liquiditySoakStatus": config.liquidity_soak_status,
                               "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    execute()
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8084), Handler).serve_forever()
