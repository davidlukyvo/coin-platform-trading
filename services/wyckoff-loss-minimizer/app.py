import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest

from engine import ResearchConfig, analyze, load_bars, load_signals, persist


LAST = Gauge("wyckoff_loss_minimizer_last_success_unixtime", "Last successful analysis")
SIGNALS = Gauge("wyckoff_loss_minimizer_source_signals", "ALLOW signals analyzed")
QUALIFIED = Gauge("wyckoff_loss_minimizer_qualified_candidates", "Qualified shadow candidates")
SEGMENT = Gauge("wyckoff_loss_minimizer_segment_value", "Walk-forward segment value", ["segment", "metric"])
ERRORS = Counter("wyckoff_loss_minimizer_errors_total", "Analysis errors", ["type"])
state = {"healthy": False, "error": None, "report": None}


config = ResearchConfig()
signals_db = Path(os.getenv("WYCKOFF_SIGNALS_DB", "/data/wyckoff/signals.db"))
silver_root = Path(os.getenv("SILVER_ROOT", "/data/silver"))
output_root = Path(os.getenv("RESEARCH_OUTPUT_ROOT", "/data/loss-minimizer"))


def execute():
    try:
        report = analyze(load_signals(signals_db, config.symbol), load_bars(silver_root, config.symbol), config)
        persist(report, output_root)
        SIGNALS.set(report["sourceSignals"])
        QUALIFIED.set(len(report["qualifiedCandidates"]))
        SEGMENT.clear()
        for segment, values in report["baseline"].items():
            for metric in ("closed", "netPnl", "maxDrawdown", "winRate"):
                SEGMENT.labels(segment, metric).set(values[metric])
        LAST.set(time.time())
        state.update(healthy=True, error=None, report=report)
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc()
        state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("RESEARCH_INTERVAL_SECONDS", "21600")))
        execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy",
                               "mode": "SHADOW_RESEARCH_ONLY", "liveTrading": False,
                               "executionActionable": False, "executionGatePassed": False,
                               "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        elif self.path == "/projection":
            body = json.dumps(state["report"] or {}).encode()
            code, kind = (200 if state["report"] else 503), "application/json"
        else:
            body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    execute()
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8085), Handler).serve_forever()
