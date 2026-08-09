from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd


UTC = timezone.utc


@dataclass(frozen=True)
class RiskLimits:
    allowed_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    max_order_notional: float = 100.0
    max_symbol_position: float = 150.0
    max_gross_exposure: float = 250.0
    max_daily_loss: float = 25.0
    max_drawdown_fraction: float = 0.02
    max_trades_per_day: int = 20
    max_market_age_seconds: int = 300


@dataclass(frozen=True)
class OrderIntent:
    signal_id: str
    symbol: str
    side: str
    notional: float
    market_price: float
    market_time: float


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


class Journal:
    def __init__(self, path: Path, starting_cash: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The engine is initialized by the main thread and its periodic cycle is
        # executed by the scheduler thread. Only that scheduler writes after
        # startup, so allow the connection to follow the engine across threads.
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, peak_equity REAL NOT NULL, day TEXT NOT NULL, day_start_equity REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, quantity REAL NOT NULL, average_entry REAL NOT NULL, mark_price REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS signals(signal_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL, desired INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT UNIQUE NOT NULL, timestamp REAL NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, requested_notional REAL NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER UNIQUE NOT NULL, timestamp REAL NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, notional REAL NOT NULL, fee REAL NOT NULL, slippage_bps REAL NOT NULL, realized_pnl REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS equity(timestamp REAL NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL, gross_exposure REAL NOT NULL, drawdown REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_events(timestamp REAL NOT NULL, signal_id TEXT NOT NULL, symbol TEXT NOT NULL, allowed INTEGER NOT NULL, reason TEXT NOT NULL);
            """
        )
        today = datetime.now(UTC).date().isoformat()
        self.connection.execute(
            "INSERT OR IGNORE INTO account(id,cash,peak_equity,day,day_start_equity) VALUES(1,?,?,?,?)",
            (starting_cash, starting_cash, today, starting_cash),
        )
        self.connection.commit()

    def account(self) -> sqlite3.Row:
        return self.connection.execute("SELECT * FROM account WHERE id=1").fetchone()

    def positions(self) -> dict[str, sqlite3.Row]:
        return {row["symbol"]: row for row in self.connection.execute("SELECT * FROM positions")}

    def processed(self, signal_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone() is not None

    def trades_today(self) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return int(self.connection.execute("SELECT count(*) FROM fills WHERE timestamp>=?", (start,)).fetchone()[0])

    def recent_fills(self, limit: int = 20) -> list[dict]:
        rows = self.connection.execute("SELECT timestamp,symbol,side,quantity,price,notional,fee,realized_pnl FROM fills ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def recent_risk_events(self, limit: int = 20) -> list[dict]:
        rows = self.connection.execute("SELECT timestamp,symbol,allowed,reason FROM risk_events ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]


class RiskEngine:
    def __init__(self, limits: RiskLimits, kill_switch: bool = False):
        self.limits = limits
        self.kill_switch = kill_switch

    def evaluate(self, intent: OrderIntent, *, equity: float, peak_equity: float, day_start_equity: float,
                 symbol_exposure: float, gross_exposure: float, trades_today: int, duplicate: bool) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, "kill_switch")
        if duplicate:
            return RiskDecision(False, "duplicate_signal")
        if intent.symbol not in self.limits.allowed_symbols:
            return RiskDecision(False, "symbol_not_allowed")
        if intent.side not in {"BUY", "SELL"}:
            return RiskDecision(False, "side_not_allowed")
        if not all(math.isfinite(x) and x >= 0 for x in (intent.notional, intent.market_price, equity, gross_exposure)):
            return RiskDecision(False, "invalid_numeric_value")
        if time.time() - intent.market_time > self.limits.max_market_age_seconds:
            return RiskDecision(False, "stale_market_data")
        if intent.notional <= 0 or intent.notional > self.limits.max_order_notional + 1e-9:
            return RiskDecision(False, "max_order_notional")
        resulting_symbol = symbol_exposure + intent.notional if intent.side == "BUY" else max(0.0, symbol_exposure - intent.notional)
        resulting_gross = gross_exposure + intent.notional if intent.side == "BUY" else max(0.0, gross_exposure - intent.notional)
        if resulting_symbol > self.limits.max_symbol_position + 1e-9:
            return RiskDecision(False, "max_symbol_position")
        if resulting_gross > self.limits.max_gross_exposure + 1e-9:
            return RiskDecision(False, "max_gross_exposure")
        if day_start_equity - equity >= self.limits.max_daily_loss:
            return RiskDecision(False, "daily_loss_limit")
        drawdown = 0 if peak_equity <= 0 else max(0.0, (peak_equity - equity) / peak_equity)
        if drawdown >= self.limits.max_drawdown_fraction:
            return RiskDecision(False, "drawdown_limit")
        if trades_today >= self.limits.max_trades_per_day:
            return RiskDecision(False, "max_trades_per_day")
        return RiskDecision(True, "allowed")


class PaperBroker:
    def __init__(self, journal: Journal, fee_bps: float = 4.0, slippage_bps: float = 2.0):
        self.journal = journal
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps

    def equity(self, prices: dict[str, float]) -> tuple[float, float]:
        account = self.journal.account()
        gross = sum(row["quantity"] * prices.get(symbol, row["mark_price"]) for symbol, row in self.journal.positions().items())
        return float(account["cash"]) + gross, gross

    def fill(self, intent: OrderIntent) -> dict:
        side_multiplier = 1 + self.slippage_bps / 10000 if intent.side == "BUY" else 1 - self.slippage_bps / 10000
        fill_price = intent.market_price * side_multiplier
        positions = self.journal.positions()
        current = positions.get(intent.symbol)
        current_qty = float(current["quantity"]) if current else 0.0
        average_entry = float(current["average_entry"]) if current else 0.0
        requested_qty = intent.notional / fill_price
        quantity = requested_qty if intent.side == "BUY" else min(current_qty, requested_qty)
        notional = quantity * fill_price
        fee = notional * self.fee_bps / 10000
        account = self.journal.account()
        cash = float(account["cash"])
        if intent.side == "BUY":
            if notional + fee > cash:
                raise ValueError("insufficient_paper_cash")
            new_qty = current_qty + quantity
            new_average = (current_qty * average_entry + notional + fee) / new_qty
            cash -= notional + fee
            realized = 0.0
        else:
            if quantity <= 0:
                raise ValueError("no_paper_position")
            new_qty = current_qty - quantity
            new_average = average_entry if new_qty > 1e-12 else 0.0
            cash += notional - fee
            realized = (fill_price - average_entry) * quantity - fee
        now = time.time()
        with self.journal.connection:
            cursor = self.journal.connection.execute(
                "INSERT INTO orders(signal_id,timestamp,symbol,side,requested_notional,status,reason) VALUES(?,?,?,?,?,'FILLED','paper_fill')",
                (intent.signal_id, now, intent.symbol, intent.side, intent.notional),
            )
            self.journal.connection.execute(
                "INSERT INTO fills(order_id,timestamp,symbol,side,quantity,price,notional,fee,slippage_bps,realized_pnl) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cursor.lastrowid, now, intent.symbol, intent.side, quantity, fill_price, notional, fee, self.slippage_bps, realized),
            )
            self.journal.connection.execute(
                "INSERT INTO positions(symbol,quantity,average_entry,mark_price,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,average_entry=excluded.average_entry,mark_price=excluded.mark_price,updated_at=excluded.updated_at",
                (intent.symbol, new_qty, new_average, intent.market_price, now),
            )
            self.journal.connection.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
        return {"quantity": quantity, "price": fill_price, "notional": notional, "fee": fee, "realized_pnl": realized}


def load_latest_bars(root: Path, exchange: str, symbol: str, limit: int = 240) -> pd.DataFrame:
    stream = "kline_1m" if exchange == "binance" else "kline_1min"
    pattern = str(root / exchange / "spot" / stream / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """SELECT open_time, close_time, close FROM read_parquet(?) WHERE is_closed
               QUALIFY row_number() OVER (PARTITION BY open_time ORDER BY exchange_event_time DESC)=1
               ORDER BY open_time DESC LIMIT ?"""
    frame = duckdb.connect().execute(query, [pattern, limit]).df().sort_values("open_time").reset_index(drop=True)
    frame["close"] = pd.to_numeric(frame["close"])
    return frame


def ema_signal(frame: pd.DataFrame) -> tuple[int, float, float]:
    if len(frame) < 30:
        raise ValueError("insufficient_bars")
    fast = frame["close"].ewm(span=12, adjust=False).mean()
    slow = frame["close"].ewm(span=26, adjust=False).mean()
    return int(fast.iloc[-1] > slow.iloc[-1]), float(frame["close"].iloc[-1]), pd.Timestamp(frame["close_time"].iloc[-1]).timestamp()


class PaperEngine:
    def __init__(self, silver_root: Path, data_root: Path, exchange: str = "binance", symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
                 starting_cash: float = 10000.0, target_notional: float = 100.0, limits: RiskLimits | None = None,
                 fee_bps: float = 4.0, slippage_bps: float = 2.0, kill_switch: bool = False, state_root: Path | None = None):
        self.silver_root, self.data_root, self.exchange, self.symbols = silver_root, data_root, exchange, symbols
        self.state_root = state_root or data_root
        self.target_notional = target_notional
        self.limits = limits or RiskLimits(allowed_symbols=symbols)
        self.journal = Journal(data_root / "paper.db", starting_cash)
        self.risk = RiskEngine(self.limits, kill_switch)
        self.broker = PaperBroker(self.journal, fee_bps, slippage_bps)

    def run_once(self) -> dict:
        prices, signals = {}, []
        for symbol in self.symbols:
            frame = load_latest_bars(self.silver_root, self.exchange, symbol)
            desired, price, market_time = ema_signal(frame)
            prices[symbol] = price
            bar_id = pd.Timestamp(frame["open_time"].iloc[-1]).isoformat()
            signal_id = hashlib.sha256(f"ema_trend:{self.exchange}:{symbol}:{bar_id}:{desired}".encode()).hexdigest()
            signals.append((signal_id, symbol, desired, price, market_time))
        equity, gross = self.broker.equity(prices)
        account = self.journal.account()
        today = datetime.now(UTC).date().isoformat()
        if account["day"] != today:
            with self.journal.connection:
                self.journal.connection.execute("UPDATE account SET day=?,day_start_equity=? WHERE id=1", (today, equity))
            account = self.journal.account()
        peak = max(float(account["peak_equity"]), equity)
        with self.journal.connection:
            self.journal.connection.execute("UPDATE account SET peak_equity=? WHERE id=1", (peak,))
        for signal_id, symbol, desired, price, market_time in signals:
            if self.journal.processed(signal_id):
                continue
            position = self.journal.positions().get(symbol)
            quantity = float(position["quantity"]) if position else 0.0
            exposure = quantity * price
            side = "BUY" if desired and exposure < self.target_notional * 0.5 else "SELL" if not desired and exposure > 1e-9 else "HOLD"
            if side == "HOLD":
                with self.journal.connection:
                    self.journal.connection.execute(
                        "INSERT INTO signals VALUES(?,?,?,?,?,?,?)", (signal_id, time.time(), symbol, "ema_trend", desired, "HOLD", "target_already_satisfied")
                    )
                continue
            notional = self.target_notional - exposure if side == "BUY" else exposure
            intent = OrderIntent(signal_id, symbol, side, notional, price, market_time)
            equity, gross = self.broker.equity(prices)
            decision = self.risk.evaluate(
                intent, equity=equity, peak_equity=peak, day_start_equity=float(account["day_start_equity"]),
                symbol_exposure=exposure, gross_exposure=gross, trades_today=self.journal.trades_today(), duplicate=False,
            )
            with self.journal.connection:
                self.journal.connection.execute(
                    "INSERT INTO risk_events VALUES(?,?,?,?,?)", (time.time(), signal_id, symbol, int(decision.allowed), decision.reason)
                )
                self.journal.connection.execute(
                    "INSERT INTO signals VALUES(?,?,?,?,?,?,?)", (signal_id, time.time(), symbol, "ema_trend", desired, "ALLOWED" if decision.allowed else "REJECTED", decision.reason)
                )
            if decision.allowed:
                self.broker.fill(intent)
        equity, gross = self.broker.equity(prices)
        peak = max(float(self.journal.account()["peak_equity"]), equity)
        drawdown = 0 if peak <= 0 else max(0.0, (peak - equity) / peak)
        with self.journal.connection:
            self.journal.connection.execute("UPDATE account SET peak_equity=? WHERE id=1", (peak,))
            self.journal.connection.execute("INSERT INTO equity VALUES(?,?,?,?,?)", (time.time(), equity, float(self.journal.account()["cash"]), gross, drawdown))
        state = {
            "mode": "PAPER_ONLY", "live_trading": False, "strategy": "ema_trend", "exchange_data": self.exchange,
            "updated_at": datetime.now(UTC).isoformat(), "cash": float(self.journal.account()["cash"]), "equity": equity,
            "gross_exposure": gross, "drawdown": drawdown, "kill_switch": self.risk.kill_switch,
            "limits": asdict(self.limits), "positions": [dict(row) for row in self.journal.positions().values()],
            "recent_fills": self.journal.recent_fills(), "recent_risk_events": self.journal.recent_risk_events(),
        }
        self.state_root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_root / "latest-state.json.tmp"
        temporary.write_text(json.dumps(state, separators=(",", ":"), default=str) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_root / "latest-state.json")
        os.chmod(self.state_root / "latest-state.json", 0o640)
        return state
