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
CASH = Gauge("paper_futures_cash", "Paper futures available virtual cash", ["strategy"])
EXPOSURE = Gauge("paper_futures_gross_notional", "Paper futures gross notional", ["strategy"])
RISK_USAGE = Gauge("paper_futures_risk_utilization_fraction", "Gross notional divided by configured maximum", ["strategy"])
CAPITAL = Gauge("paper_futures_capital", "Paper futures capital and planned outcome values", ["strategy", "metric"])
DRAWDOWN = Gauge("paper_futures_drawdown", "Paper futures drawdown", ["strategy"])
POSITIONS = Gauge("paper_futures_positions", "Paper futures open positions", ["strategy", "side"])
LIQUIDATIONS = Gauge("paper_futures_liquidations_total", "Paper futures simulated liquidations", ["strategy"])
POSITION_PRICE = Gauge("paper_futures_position_price", "Paper futures position price levels", ["strategy", "symbol", "side", "level"])
UNREALIZED = Gauge("paper_futures_unrealized_pnl", "Paper futures position unrealized PnL", ["strategy", "symbol", "side"])
SIGNAL_INFO = Gauge("paper_futures_latest_signal_info", "Latest paper futures strategy decision", ["strategy", "symbol", "direction", "decision", "reason"])
RISK_INFO = Gauge("paper_futures_latest_risk_info", "Latest paper futures risk decision", ["strategy", "symbol", "direction", "decision", "reason"])
FILL_INFO = Gauge("paper_futures_latest_fill_info", "Latest paper futures fill metadata", ["strategy", "symbol", "action", "side", "reason"])
FILL_VALUE = Gauge("paper_futures_latest_fill_value", "Latest paper futures fill values", ["strategy", "symbol", "action", "side", "field"])
POSITION_INFO = Gauge("paper_futures_position_info", "Currently open paper futures positions", ["strategy", "symbol", "side", "status"])
POSITION_PROGRESS = Gauge("paper_futures_position_progress_percent", "Distance and return metrics for open paper futures positions", ["strategy", "symbol", "side", "metric"])
POSITION_DETAIL = Gauge("paper_futures_position_detail", "Position margin, notional and risk reward", ["strategy", "symbol", "side", "metric"])
SUMMARY = Gauge("paper_futures_strategy_summary", "Paper futures cumulative strategy summary", ["strategy", "metric"])
CLOSED_INFO = Gauge("paper_futures_closed_trade_info", "Recent closed paper futures trades", ["trade_id", "closed_at", "strategy", "symbol", "side", "exit_reason"])
CLOSED_VALUE = Gauge("paper_futures_closed_trade_value", "Recent closed paper futures trade values", ["trade_id", "symbol", "side", "field"])
ERRORS = Counter("paper_futures_errors_total", "Paper futures cycle errors", ["type"])
state = {"healthy": False, "error": None}


def number(name, default, cast=float): return cast(os.getenv(name, str(default)))


def boolean(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


config = FuturesConfig(
    leverage=number("FUTURES_LEVERAGE", 10), starting_equity=number("FUTURES_STARTING_EQUITY", 10000),
    margin_per_trade=number("FUTURES_MARGIN_PER_TRADE", 100), fee_bps=number("FUTURES_FEE_BPS", 5),
    personal_income_tax_bps=number("FUTURES_PERSONAL_INCOME_TAX_BPS", 10),
    slippage_bps=number("FUTURES_SLIPPAGE_BPS", 2), maintenance_margin_fraction=number("FUTURES_MAINTENANCE_MARGIN", 0.005),
    ema_stop_fraction=number("FUTURES_EMA_STOP_FRACTION", 0.01), ema_target_fraction=number("FUTURES_EMA_TARGET_FRACTION", 0.02),
    max_gross_notional=number("FUTURES_MAX_GROSS_NOTIONAL", 2000), max_daily_loss=number("FUTURES_MAX_DAILY_LOSS", 100),
    max_drawdown_fraction=number("FUTURES_MAX_DRAWDOWN_FRACTION", 0.05), max_trades_per_day=number("FUTURES_MAX_TRADES_PER_DAY", 30, int),
    max_market_age_seconds=number("FUTURES_MAX_MARKET_AGE_SECONDS", 3900, int),
    ema_entry_enabled=boolean("FUTURES_EMA_ENTRY_ENABLED", False),
    ema_gate_persistence_bars=number("FUTURES_EMA_GATE_PERSISTENCE_BARS", 3, int),
    ema_gate_min_separation_bps=number("FUTURES_EMA_GATE_MIN_SEPARATION_BPS", 5),
    ema_gate_min_slope_bps=number("FUTURES_EMA_GATE_MIN_SLOPE_BPS", 2),
    ema_gate_max_extension_bps=number("FUTURES_EMA_GATE_MAX_EXTENSION_BPS", 50),
    ema_gate_min_net_rr=number("FUTURES_EMA_GATE_MIN_NET_RR", 1.5),
)
def symbols(name: str, fallback: str) -> tuple[str, ...]:
    return tuple(x.strip().upper() for x in os.getenv(name, fallback).split(",") if x.strip())


legacy_symbols = symbols("FUTURES_SYMBOLS", "BTCUSDT,ETHUSDT")
ema_symbols = symbols("FUTURES_EMA_SYMBOLS", ",".join(legacy_symbols))
wyckoff_symbols = symbols("FUTURES_WYCKOFF_SYMBOLS", ",".join(legacy_symbols))
all_symbols = tuple(dict.fromkeys((*ema_symbols, *wyckoff_symbols)))
engine = FuturesEngine(Path(os.getenv("SILVER_ROOT", "/data/silver")), Path(os.getenv("WYCKOFF_ROOT", "/data/wyckoff")),
                       Path(os.getenv("FUTURES_DATA_ROOT", "/data/paper-futures")), os.getenv("FUTURES_DATA_EXCHANGE", "binance"),
                       all_symbols, config, ema_symbols=ema_symbols, wyckoff_symbols=wyckoff_symbols)


def execute():
    try:
        result = engine.run_once()
        POSITIONS.clear(); POSITION_PRICE.clear(); UNREALIZED.clear(); POSITION_DETAIL.clear(); CAPITAL.clear(); SIGNAL_INFO.clear(); RISK_INFO.clear()
        FILL_INFO.clear(); FILL_VALUE.clear(); POSITION_INFO.clear(); POSITION_PROGRESS.clear(); SUMMARY.clear()
        CLOSED_INFO.clear(); CLOSED_VALUE.clear()
        for account in result["accounts"]:
            name = account["strategy"]; EQUITY.labels(name).set(account["equity"]); CASH.labels(name).set(account["cash"])
            EXPOSURE.labels(name).set(account["gross_notional"])
            RISK_USAGE.labels(name).set(account["gross_notional"] / config.max_gross_notional)
            DRAWDOWN.labels(name).set(account["drawdown"]); LIQUIDATIONS.labels(name).set(account["summary"]["liquidations"])
            used_margin = sum(float(x["margin"]) for x in account["positions"])
            maximum_loss = expected_gain = 0.0
            for x in account["positions"]:
                quantity = float(x["quantity"]); close_fee_stop = quantity * float(x["stop"]) * config.fee_bps / 10000
                close_fee_target = quantity * float(x["target"]) * config.fee_bps / 10000
                close_tax_stop = quantity * float(x["stop"]) * config.personal_income_tax_bps / 10000 if x["side"] == "LONG" else 0.0
                close_tax_target = quantity * float(x["target"]) * config.personal_income_tax_bps / 10000 if x["side"] == "LONG" else 0.0
                maximum_loss += quantity * abs(float(x["entry"]) - float(x["stop"])) + close_fee_stop + close_tax_stop
                expected_gain += max(0.0, quantity * abs(float(x["target"]) - float(x["entry"])) - close_fee_target - close_tax_target)
            available_capital = float(account["equity"]) - used_margin
            daily_loss_remaining = max(0.0, config.max_daily_loss + float(account["summary"]["realized"]))
            for metric, value in (("used_margin", used_margin), ("available_capital", available_capital),
                                  ("maximum_loss", maximum_loss), ("expected_gain", expected_gain),
                                  ("daily_loss_remaining", daily_loss_remaining)):
                CAPITAL.labels(name, metric).set(value)
            for side in ("LONG", "SHORT"):
                POSITIONS.labels(name, side).set(sum(1 for x in account["positions"] if x["side"] == side))
            for position in account["positions"]:
                base_labels = (name, position["symbol"], position["side"])
                status = "WINNING" if float(position["unrealized_pnl"]) > 0 else "LOSING" if float(position["unrealized_pnl"]) < 0 else "FLAT"
                POSITION_INFO.labels(*base_labels, status).set(1)
                labels = base_labels
                for level in ("entry", "mark", "stop", "target", "liquidation"):
                    POSITION_PRICE.labels(*labels, level).set(position[level])
                UNREALIZED.labels(*labels).set(position["unrealized_pnl"])
                entry, mark = float(position["entry"]), float(position["mark"])
                direction = 1.0 if position["side"] == "LONG" else -1.0
                roe = direction * (mark - entry) / entry * config.leverage * 100
                stop_distance = direction * (mark - float(position["stop"])) / mark * 100
                target_distance = direction * (float(position["target"]) - mark) / mark * 100
                liquidation_distance = direction * (mark - float(position["liquidation"])) / mark * 100
                for metric, value in (("roe", roe), ("to_stop", stop_distance),
                                      ("to_target", target_distance), ("to_liquidation", liquidation_distance)):
                    POSITION_PROGRESS.labels(*labels, metric).set(value)
                risk = abs(entry - float(position["stop"])); reward = abs(float(position["target"]) - entry)
                notional = float(position["quantity"]) * mark
                for metric, value in (("margin", float(position["margin"])), ("notional", notional),
                                      ("risk_reward", reward / risk if risk else 0.0)):
                    POSITION_DETAIL.labels(*labels, metric).set(value)
            for metric in ("signals", "fills", "closed", "wins", "fees", "taxes", "gross_realized", "realized", "liquidations", "win_rate"):
                SUMMARY.labels(name, metric).set(account["summary"][metric])
        latest_signals = {}
        latest_risk = {}
        for item in result["recentSignals"]:
            key = (item["strategy"], item["symbol"])
            latest_signals.setdefault(key, item)
            if item["decision"] in {"ALLOWED", "REJECTED"}:
                latest_risk.setdefault(key, item)
        for item in latest_signals.values():
            SIGNAL_INFO.labels(item["strategy"], item["symbol"], item["direction"], item["decision"], item["reason"]).set(1)
        for item in latest_risk.values():
            RISK_INFO.labels(item["strategy"], item["symbol"], item["direction"], item["decision"], item["reason"]).set(1)
        latest_fills = {}
        for item in result["recentFills"]:
            latest_fills.setdefault((item["strategy"], item["symbol"]), item)
        for item in latest_fills.values():
            labels = (item["strategy"], item["symbol"], item["action"], item["side"])
            FILL_INFO.labels(*labels, item["reason"]).set(1)
            for field in ("price", "notional", "fee", "tax", "gross_pnl", "pnl"):
                FILL_VALUE.labels(*labels, field).set(item[field])
        for item in result["recentClosedTrades"]:
            trade_id = str(item["id"]); closed_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(item["timestamp"]))
            CLOSED_INFO.labels(trade_id, closed_at, item["strategy"], item["symbol"], item["side"], item["reason"]).set(1)
            for field in ("entry_price", "exit_price", "gross_pnl", "fee", "tax", "pnl"):
                CLOSED_VALUE.labels(trade_id, item["symbol"], item["side"], field).set(float(item[field] or 0))
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
                               "liveTrading": False, "leverage": 10, "marginMode": "ISOLATED",
                               "emaEntryMode": "PAPER" if config.ema_entry_enabled else "RESEARCH_ONLY",
                               "emaEntryGateVersion": "ema_entry_gate_v2", "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute(); threading.Thread(target=scheduler, daemon=True).start(); ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
