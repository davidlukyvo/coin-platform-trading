import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Gauge, generate_latest
from research import run

NET = Gauge("strategy_backtest_net_return", "Net return", ["symbol", "strategy", "segment", "confidence"])
DD = Gauge("strategy_backtest_max_drawdown", "Maximum drawdown", ["symbol", "strategy", "segment"])
SHARPE = Gauge("strategy_backtest_sharpe", "Annualized Sharpe", ["symbol", "strategy", "segment"])
TRADES = Gauge("strategy_backtest_trades", "Completed trades", ["symbol", "strategy", "segment"])
LAST = Gauge("strategy_research_last_success_unixtime", "Last successful research run")
ERRORS = Gauge("strategy_research_errors", "Research errors")
state = {"healthy": False, "error": None}


def execute():
    try:
        for r in run(Path(os.getenv("SILVER_ROOT", "/data/silver")), Path(os.getenv("RESEARCH_ROOT", "/data/research"))):
            NET.labels(r.symbol, r.strategy, r.segment, r.confidence).set(r.net_return)
            DD.labels(r.symbol, r.strategy, r.segment).set(r.max_drawdown)
            SHARPE.labels(r.symbol, r.strategy, r.segment).set(r.sharpe)
            TRADES.labels(r.symbol, r.strategy, r.segment).set(r.trades)
        LAST.set(time.time()); ERRORS.set(0); state.update(healthy=True, error=None)
    except Exception as exc:
        ERRORS.set(1); state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("RESEARCH_INTERVAL_SECONDS", "21600")))
        execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz": body, code, kind = ((b'{"status":"healthy"}' if state["healthy"] else b'{"status":"unhealthy"}'), (200 if state["healthy"] else 503), "application/json")
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute()
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8020), Handler).serve_forever()
