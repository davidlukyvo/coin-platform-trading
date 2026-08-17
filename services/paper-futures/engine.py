from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd


@dataclass(frozen=True)
class FuturesConfig:
    leverage: float = 10.0
    starting_equity: float = 10000.0
    margin_per_trade: float = 100.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    maintenance_margin_fraction: float = 0.005
    ema_stop_fraction: float = 0.01
    ema_target_fraction: float = 0.02
    max_gross_notional: float = 2000.0
    max_daily_loss: float = 100.0
    max_drawdown_fraction: float = 0.05
    max_trades_per_day: int = 30
    max_market_age_seconds: int = 3900


def liquidation_price(entry: float, side: str, leverage: float, maintenance: float) -> float:
    distance = max(0.0, 1.0 / leverage - maintenance)
    return entry * (1.0 - distance if side == "LONG" else 1.0 + distance)


def position_pnl(side: str, quantity: float, entry: float, mark: float) -> float:
    return quantity * (mark - entry) * (1.0 if side == "LONG" else -1.0)


def load_latest_bars(root: Path, exchange: str, symbol: str, limit: int = 240) -> pd.DataFrame:
    stream = "kline_1m" if exchange == "binance" else "kline_1min"
    pattern = str(root / exchange / "spot" / stream / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """SELECT open_time,close_time,close FROM read_parquet(?) WHERE is_closed
               QUALIFY row_number() OVER(PARTITION BY open_time ORDER BY exchange_event_time DESC)=1
               ORDER BY open_time DESC LIMIT ?"""
    frame = duckdb.connect().execute(query, [pattern, limit]).df().sort_values("open_time").reset_index(drop=True)
    frame["close"] = pd.to_numeric(frame["close"])
    return frame


def ema_direction(frame: pd.DataFrame) -> tuple[str, float, float, str]:
    if len(frame) < 30:
        raise ValueError("insufficient_bars")
    fast = frame["close"].ewm(span=12, adjust=False).mean().iloc[-1]
    slow = frame["close"].ewm(span=26, adjust=False).mean().iloc[-1]
    return ("LONG" if fast > slow else "SHORT", float(frame["close"].iloc[-1]),
            pd.Timestamp(frame["close_time"].iloc[-1]).timestamp(), pd.Timestamp(frame["open_time"].iloc[-1]).isoformat())


class FuturesJournal:
    def __init__(self, path: Path, strategies: tuple[str, ...], starting_equity: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS accounts(strategy TEXT PRIMARY KEY,cash REAL NOT NULL,peak REAL NOT NULL,day TEXT NOT NULL,day_start REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS positions(strategy TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity REAL NOT NULL,entry REAL NOT NULL,mark REAL NOT NULL,margin REAL NOT NULL,stop REAL NOT NULL,target REAL NOT NULL,liquidation REAL NOT NULL,opened_at REAL NOT NULL,PRIMARY KEY(strategy,symbol));
        CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,timestamp REAL NOT NULL,strategy TEXT NOT NULL,symbol TEXT NOT NULL,direction TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL NOT NULL,strategy TEXT NOT NULL,symbol TEXT NOT NULL,action TEXT NOT NULL,side TEXT NOT NULL,quantity REAL NOT NULL,price REAL NOT NULL,notional REAL NOT NULL,fee REAL NOT NULL,pnl REAL NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS equity(timestamp REAL NOT NULL,strategy TEXT NOT NULL,equity REAL NOT NULL,gross_notional REAL NOT NULL,drawdown REAL NOT NULL);
        """)
        today = datetime.now(UTC).date().isoformat()
        with self.db:
            for strategy in strategies:
                self.db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?)", (strategy, starting_equity, starting_equity, today, starting_equity))

    def account(self, strategy: str):
        return self.db.execute("SELECT * FROM accounts WHERE strategy=?", (strategy,)).fetchone()

    def position(self, strategy: str, symbol: str):
        return self.db.execute("SELECT * FROM positions WHERE strategy=? AND symbol=?", (strategy, symbol)).fetchone()

    def processed(self, signal_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM signals WHERE id=?", (signal_id,)).fetchone() is not None

    def trades_today(self, strategy: str) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return int(self.db.execute("SELECT count(*) FROM fills WHERE strategy=? AND timestamp>=?", (strategy, start)).fetchone()[0])

    def positions(self, strategy: str) -> list[dict]:
        return [dict(x) for x in self.db.execute("SELECT * FROM positions WHERE strategy=? ORDER BY symbol", (strategy,))]

    def recent(self, table: str, limit: int = 30) -> list[dict]:
        if table not in {"signals", "fills"}:
            raise ValueError("invalid_table")
        return [dict(x) for x in self.db.execute(f"SELECT * FROM {table} ORDER BY timestamp DESC LIMIT ?", (limit,))]

    def summary(self, strategy: str) -> dict:
        row = self.db.execute("""SELECT count(*) fills,coalesce(sum(fee),0) fees,coalesce(sum(pnl),0) realized,
          sum(CASE WHEN action='CLOSE' THEN 1 ELSE 0 END) closed,
          sum(CASE WHEN action='CLOSE' AND pnl>0 THEN 1 ELSE 0 END) wins,
          sum(CASE WHEN action='LIQUIDATION' THEN 1 ELSE 0 END) liquidations
          FROM fills WHERE strategy=?""", (strategy,)).fetchone()
        result = dict(row); result["closed"] = int(result["closed"] or 0); result["wins"] = int(result["wins"] or 0)
        result["liquidations"] = int(result["liquidations"] or 0)
        result["win_rate"] = result["wins"] / result["closed"] if result["closed"] else 0.0
        result["signals"] = int(self.db.execute("SELECT count(*) FROM signals WHERE strategy=?", (strategy,)).fetchone()[0])
        return result

    def recent_closed_trades(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("""
          SELECT c.id,c.timestamp,c.strategy,c.symbol,c.side,c.price AS exit_price,c.fee,c.pnl,c.reason,
                 (SELECT o.price FROM fills o WHERE o.strategy=c.strategy AND o.symbol=c.symbol
                    AND o.action='OPEN' AND o.timestamp<c.timestamp ORDER BY o.timestamp DESC LIMIT 1) AS entry_price
          FROM fills c WHERE c.action IN ('CLOSE','LIQUIDATION') ORDER BY c.id DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in rows]


class FuturesEngine:
    strategies = ("ema_trend_x10", "wyckoff_x10")

    def __init__(self, silver_root: Path, wyckoff_root: Path, data_root: Path, exchange: str,
                 symbols: tuple[str, ...], config: FuturesConfig,
                 ema_symbols: tuple[str, ...] | None = None,
                 wyckoff_symbols: tuple[str, ...] | None = None):
        self.silver_root, self.wyckoff_root, self.data_root = silver_root, wyckoff_root, data_root
        self.exchange, self.symbols, self.config = exchange, symbols, config
        self.strategy_symbols = {
            "ema_trend_x10": tuple(ema_symbols or symbols),
            "wyckoff_x10": tuple(wyckoff_symbols or symbols),
        }
        self.journal = FuturesJournal(data_root / "paper-futures.db", self.strategies, config.starting_equity)

    def _equity(self, strategy: str, prices: dict[str, float]) -> tuple[float, float]:
        account = self.journal.account(strategy); unrealized = gross = 0.0
        for row in self.journal.positions(strategy):
            mark = prices.get(row["symbol"], row["mark"]); unrealized += position_pnl(row["side"], row["quantity"], row["entry"], mark)
            gross += row["quantity"] * mark
        return float(account["cash"]) + unrealized, gross

    def _close(self, strategy: str, symbol: str, price: float, action: str, reason: str) -> None:
        row = self.journal.position(strategy, symbol)
        if not row: return
        adverse = -self.config.slippage_bps / 10000 if row["side"] == "LONG" else self.config.slippage_bps / 10000
        fill = price * (1 + adverse); notional = row["quantity"] * fill; fee = notional * self.config.fee_bps / 10000
        pnl = position_pnl(row["side"], row["quantity"], row["entry"], fill) - fee
        with self.journal.db:
            self.journal.db.execute("UPDATE accounts SET cash=cash+? WHERE strategy=?", (pnl, strategy))
            self.journal.db.execute("DELETE FROM positions WHERE strategy=? AND symbol=?", (strategy, symbol))
            self.journal.db.execute("INSERT INTO fills(timestamp,strategy,symbol,action,side,quantity,price,notional,fee,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (time.time(), strategy, symbol, action, row["side"], row["quantity"], fill, notional, fee, pnl, reason))

    def _open(self, strategy: str, symbol: str, side: str, price: float, stop: float, target: float) -> None:
        slip = self.config.slippage_bps / 10000 if side == "LONG" else -self.config.slippage_bps / 10000
        fill = price * (1 + slip); notional = self.config.margin_per_trade * self.config.leverage
        quantity = notional / fill; fee = notional * self.config.fee_bps / 10000
        liquidation = liquidation_price(fill, side, self.config.leverage, self.config.maintenance_margin_fraction)
        with self.journal.db:
            self.journal.db.execute("UPDATE accounts SET cash=cash-? WHERE strategy=?", (fee, strategy))
            self.journal.db.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (strategy, symbol, side, quantity, fill, price, self.config.margin_per_trade, stop, target, liquidation, time.time()))
            self.journal.db.execute("INSERT INTO fills(timestamp,strategy,symbol,action,side,quantity,price,notional,fee,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                    (time.time(), strategy, symbol, "OPEN", side, quantity, fill, notional, fee, -fee, "signal_entry"))

    def _allowed(self, strategy: str, prices: dict[str, float]) -> tuple[bool, str]:
        equity, gross = self._equity(strategy, prices); account = self.journal.account(strategy)
        if gross + self.config.margin_per_trade * self.config.leverage > self.config.max_gross_notional: return False, "max_gross_notional"
        if float(account["day_start"]) - equity >= self.config.max_daily_loss: return False, "daily_loss_limit"
        if float(account["peak"]) > 0 and (float(account["peak"]) - equity) / float(account["peak"]) >= self.config.max_drawdown_fraction: return False, "drawdown_limit"
        if self.journal.trades_today(strategy) >= self.config.max_trades_per_day: return False, "max_trades_per_day"
        return True, "allowed"

    def _wyckoff(self) -> dict[str, dict]:
        path = self.wyckoff_root / "latest-signals.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("mode") != "SHADOW_ONLY" or state.get("liveTrading") is not False: raise ValueError("unsafe_wyckoff_projection")
        return {x["symbol"]: x for x in state.get("signals", [])}

    def run_once(self) -> dict:
        prices, ema, frames = {}, {}, {}
        for symbol in self.symbols:
            frames[symbol] = load_latest_bars(self.silver_root, self.exchange, symbol)
            direction, price, market_time, bar = ema_direction(frames[symbol]); prices[symbol] = price
            ema[symbol] = {"direction": direction, "price": price, "market_time": market_time, "bar": bar}
        wyckoff = self._wyckoff()
        for strategy in self.strategies:
            for symbol in self.strategy_symbols[strategy]:
                price = prices[symbol]; current = self.journal.position(strategy, symbol)
                if current:
                    liquidated = (current["side"] == "LONG" and price <= current["liquidation"]) or (current["side"] == "SHORT" and price >= current["liquidation"])
                    stopped = (current["side"] == "LONG" and price <= current["stop"]) or (current["side"] == "SHORT" and price >= current["stop"])
                    targeted = (current["side"] == "LONG" and price >= current["target"]) or (current["side"] == "SHORT" and price <= current["target"])
                    if liquidated: self._close(strategy, symbol, current["liquidation"], "LIQUIDATION", "isolated_liquidation")
                    elif stopped: self._close(strategy, symbol, current["stop"], "CLOSE", "stop_loss")
                    elif targeted: self._close(strategy, symbol, current["target"], "CLOSE", "take_profit")
                if strategy == "ema_trend_x10":
                    item = ema[symbol]; direction, bar, market_time = item["direction"], item["bar"], item["market_time"]
                    stop = price * (1 - self.config.ema_stop_fraction if direction == "LONG" else 1 + self.config.ema_stop_fraction)
                    target = price * (1 + self.config.ema_target_fraction if direction == "LONG" else 1 - self.config.ema_target_fraction)
                    actionable, source_reason = True, "ema_direction"
                else:
                    item = wyckoff.get(symbol, {}); direction = item.get("direction", "NONE"); bar = item.get("barId", "missing")
                    market_time = datetime.fromisoformat(item.get("marketTime", "1970-01-01T00:00:00+00:00")).timestamp()
                    stop, target = float(item.get("stop", price)), float(item.get("target", price))
                    actionable = item.get("authorityDecision") == "ALLOW" and direction in {"LONG", "SHORT"}
                    source_reason = item.get("authorityReason", "no_signal")
                signal_id = hashlib.sha256(f"paper_futures_v1:{strategy}:{symbol}:{bar}:{direction}".encode()).hexdigest()
                if self.journal.processed(signal_id): continue
                current = self.journal.position(strategy, symbol); decision, reason = "HOLD", source_reason
                if time.time() - market_time > self.config.max_market_age_seconds: decision, reason = "REJECTED", "stale_market_data"
                elif not actionable: decision, reason = "WAIT", source_reason
                elif current and current["side"] == direction: decision, reason = "HOLD", "position_already_aligned"
                else:
                    if current: self._close(strategy, symbol, price, "CLOSE", "signal_flip")
                    allowed, reason = self._allowed(strategy, prices)
                    if allowed: self._open(strategy, symbol, direction, price, stop, target); decision = "ALLOWED"
                    else: decision = "REJECTED"
                with self.journal.db:
                    self.journal.db.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?)", (signal_id, time.time(), strategy, symbol, direction, decision, reason))
        accounts = []
        for strategy in self.strategies:
            equity, gross = self._equity(strategy, prices); account = self.journal.account(strategy); peak = max(float(account["peak"]), equity)
            drawdown = max(0.0, (peak - equity) / peak) if peak else 0.0
            with self.journal.db:
                self.journal.db.execute("UPDATE accounts SET peak=? WHERE strategy=?", (peak, strategy))
                self.journal.db.execute("INSERT INTO equity VALUES(?,?,?,?,?)", (time.time(), strategy, equity, gross, drawdown))
            positions = self.journal.positions(strategy)
            for row in positions: row["unrealized_pnl"] = position_pnl(row["side"], row["quantity"], row["entry"], prices[row["symbol"]])
            accounts.append({"strategy": strategy, "equity": equity, "cash": float(account["cash"]), "gross_notional": gross,
                             "drawdown": drawdown, "positions": positions, "summary": self.journal.summary(strategy)})
        state = {"mode": "PAPER_FUTURES_ONLY", "liveTrading": False, "executionActionable": False,
                 "leverage": self.config.leverage, "marginMode": "ISOLATED", "fundingModel": "NOT_INSTRUMENTED",
                 "updatedAt": datetime.now(UTC).isoformat(), "config": asdict(self.config), "accounts": accounts,
                 "recentSignals": self.journal.recent("signals"), "recentFills": self.journal.recent("fills"),
                 "recentClosedTrades": self.journal.recent_closed_trades()}
        self.data_root.mkdir(parents=True, exist_ok=True); tmp = self.data_root / "latest-state.json.tmp"
        tmp.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8"); os.replace(tmp, self.data_root / "latest-state.json")
        os.chmod(self.data_root / "latest-state.json", 0o600); os.chmod(self.data_root / "paper-futures.db", 0o600)
        return state
