import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest

from engine import FuturesConfig, FuturesEngine


os.umask(0o077)
LAST = Gauge("paper_futures_last_success_unixtime", "Last successful x10 futures paper cycle")
EQUITY = Gauge("paper_futures_equity", "Paper futures equity", ["strategy"])
EXPOSURE = Gauge("paper_futures_gross_notional", "Paper futures gross notional", ["strategy"])
DRAWDOWN = Gauge("paper_futures_drawdown", "Paper futures drawdown", ["strategy"])
POSITIONS = Gauge("paper_futures_positions", "Paper futures open positions", ["strategy", "side"])
LIQUIDATIONS = Gauge("paper_futures_liquidations_total", "Paper futures simulated liquidations", ["strategy"])
POSITION_PRICE = Gauge("paper_futures_position_price", "Paper futures position price levels", ["strategy", "symbol", "side", "level"])
UNREALIZED = Gauge("paper_futures_unrealized_pnl", "Paper futures position unrealized PnL", ["strategy", "symbol", "side"])
ERRORS = Counter("paper_futures_errors_total", "Paper futures cycle errors", ["type"])
state = {"healthy": False, "error": None}


def number(name, default, cast=float): return cast(os.getenv(name, str(default)))


config = FuturesConfig(
    leverage=number("FUTURES_LEVERAGE", 10), starting_equity=number("FUTURES_STARTING_EQUITY", 10000),
    margin_per_trade=number("FUTURES_MARGIN_PER_TRADE", 100), fee_bps=number("FUTURES_FEE_BPS", 5),
    slippage_bps=number("FUTURES_SLIPPAGE_BPS", 2), maintenance_margin_fraction=number("FUTURES_MAINTENANCE_MARGIN", 0.005),
    ema_stop_fraction=number("FUTURES_EMA_STOP_FRACTION", 0.01), ema_target_fraction=number("FUTURES_EMA_TARGET_FRACTION", 0.02),
    max_gross_notional=number("FUTURES_MAX_GROSS_NOTIONAL", 2000), max_daily_loss=number("FUTURES_MAX_DAILY_LOSS", 100),
    max_drawdown_fraction=number("FUTURES_MAX_DRAWDOWN_FRACTION", 0.05), max_trades_per_day=number("FUTURES_MAX_TRADES_PER_DAY", 30, int),
    max_market_age_seconds=number("FUTURES_MAX_MARKET_AGE_SECONDS", 3900, int),
)
engine = FuturesEngine(Path(os.getenv("SILVER_ROOT", "/data/silver")), Path(os.getenv("WYCKOFF_ROOT", "/data/wyckoff")),
                       Path(os.getenv("FUTURES_DATA_ROOT", "/data/paper-futures")), os.getenv("FUTURES_DATA_EXCHANGE", "binance"),
                       tuple(x.strip().upper() for x in os.getenv("FUTURES_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if x.strip()), config)


def execute():
    try:
        result = engine.run_once()
        POSITIONS.clear(); POSITION_PRICE.clear(); UNREALIZED.clear()
        for account in result["accounts"]:
            name = account["strategy"]; EQUITY.labels(name).set(account["equity"]); EXPOSURE.labels(name).set(account["gross_notional"])
            DRAWDOWN.labels(name).set(account["drawdown"]); LIQUIDATIONS.labels(name).set(account["summary"]["liquidations"])
            for side in ("LONG", "SHORT"):
                POSITIONS.labels(name, side).set(sum(1 for x in account["positions"] if x["side"] == side))
            for position in account["positions"]:
                labels = (name, position["symbol"], position["side"])
                for level in ("entry", "mark", "stop", "target", "liquidation"):
                    POSITION_PRICE.labels(*labels, level).set(position[level])
                UNREALIZED.labels(*labels).set(position["unrealized_pnl"])
        LAST.set(time.time()); state.update(healthy=True, error=None)
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc(); state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True: time.sleep(number("FUTURES_INTERVAL_SECONDS", 60, int)); execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy", "mode": "PAPER_FUTURES_ONLY",
                               "liveTrading": False, "leverage": 10, "marginMode": "ISOLATED", "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute(); threading.Thread(target=scheduler, daemon=True).start(); ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
