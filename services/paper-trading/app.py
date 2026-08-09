import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import PaperEngine, RiskLimits
from prometheus_client import Counter, Gauge, generate_latest


os.umask(0o077)

EQUITY = Gauge("paper_trading_equity", "Paper account equity")
EXPOSURE = Gauge("paper_trading_gross_exposure", "Paper gross exposure")
DRAWDOWN = Gauge("paper_trading_drawdown", "Paper drawdown fraction")
POSITIONS = Gauge("paper_trading_positions", "Paper open position count")
LAST = Gauge("paper_trading_last_success_unixtime", "Last successful paper cycle")
ERRORS = Counter("paper_trading_errors_total", "Paper trading cycle errors", ["type"])
state = {"healthy": False, "error": None, "last_success": 0.0}


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


limits = RiskLimits(
    allowed_symbols=tuple(x.strip().upper() for x in os.getenv("PAPER_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if x.strip()),
    max_order_notional=env_float("RISK_MAX_ORDER_NOTIONAL", 100),
    max_symbol_position=env_float("RISK_MAX_SYMBOL_POSITION", 150),
    max_gross_exposure=env_float("RISK_MAX_GROSS_EXPOSURE", 250),
    max_daily_loss=env_float("RISK_MAX_DAILY_LOSS", 25),
    max_drawdown_fraction=env_float("RISK_MAX_DRAWDOWN_FRACTION", 0.02),
    max_trades_per_day=int(os.getenv("RISK_MAX_TRADES_PER_DAY", "20")),
    max_market_age_seconds=int(os.getenv("RISK_MAX_MARKET_AGE_SECONDS", "3900")),
)
engine = PaperEngine(
    Path(os.getenv("SILVER_ROOT", "/data/silver")), Path(os.getenv("PAPER_DATA_ROOT", "/data/paper")),
    exchange=os.getenv("PAPER_DATA_EXCHANGE", "binance"), symbols=limits.allowed_symbols,
    starting_cash=env_float("PAPER_STARTING_CASH", 10000), target_notional=env_float("PAPER_TARGET_NOTIONAL", 100),
    limits=limits, fee_bps=env_float("PAPER_FEE_BPS", 4), slippage_bps=env_float("PAPER_SLIPPAGE_BPS", 2),
    kill_switch=os.getenv("PAPER_KILL_SWITCH", "false").lower() == "true",
)


def execute():
    try:
        result = engine.run_once()
        EQUITY.set(result["equity"]); EXPOSURE.set(result["gross_exposure"]); DRAWDOWN.set(result["drawdown"])
        POSITIONS.set(sum(1 for item in result["positions"] if float(item["quantity"]) > 0))
        LAST.set(time.time()); state.update(healthy=True, error=None, last_success=time.time())
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc(); state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("PAPER_INTERVAL_SECONDS", "60")))
        execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy", "mode": "PAPER_ONLY", "live_trading": False, "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute()
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8050), Handler).serve_forever()
