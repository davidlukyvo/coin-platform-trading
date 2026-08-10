from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd


@dataclass(frozen=True)
class Thresholds:
    max_market_age_seconds: int = 3900
    range_bars: int = 20
    min_range_atr: float = 3.0
    max_range_atr: float = 15.0
    event_volume_floor: float = 1.0
    test_volume_ceiling: float = 1.0
    min_rr: float = 2.0
    ready_score: int = 70


def load_closed_minutes(root: Path, exchange: str, symbol: str, limit: int = 6500) -> pd.DataFrame:
    stream = "kline_1m" if exchange == "binance" else "kline_1min"
    pattern = str(root / exchange / "spot" / stream / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """SELECT open_time,close_time,open,high,low,close,volume,quote_volume,trade_count
               FROM read_parquet(?) WHERE is_closed
               QUALIFY row_number() OVER (PARTITION BY open_time ORDER BY exchange_event_time DESC)=1
               ORDER BY open_time DESC LIMIT ?"""
    frame = duckdb.connect().execute(query, [pattern, limit]).df().sort_values("open_time").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume", "quote_volume", "trade_count"):
        frame[column] = pd.to_numeric(frame[column])
    return frame


def resample_closed(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    source = frame.copy()
    source["open_time"] = pd.to_datetime(source["open_time"], utc=True)
    source["close_time"] = pd.to_datetime(source["close_time"], utc=True)
    result = source.set_index("open_time").resample(rule, label="left", closed="left", origin="epoch").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), quote_volume=("quote_volume", "sum"), trade_count=("trade_count", "sum"),
        close_time=("close_time", "max"), bars=("close", "count"),
    ).dropna(subset=["open", "high", "low", "close"])
    expected = int(pd.Timedelta(rule) / pd.Timedelta("1min"))
    result = result[result["bars"] == expected].reset_index()
    return result


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous = frame["close"].shift(1)
    ranges = pd.concat([
        frame["high"] - frame["low"], (frame["high"] - previous).abs(), (frame["low"] - previous).abs()
    ], axis=1).max(axis=1)
    return ranges.rolling(period).mean()


def relative_volume(frame: pd.DataFrame, index: int, period: int = 20) -> float:
    start = max(0, index - period)
    average = float(frame["volume"].iloc[start:index].mean())
    return 0.0 if not math.isfinite(average) or average <= 0 else float(frame["volume"].iloc[index] / average)


def raw_event(frame: pd.DataFrame, index: int, limits: Thresholds) -> str:
    if index < limits.range_bars:
        return "NONE"
    row = frame.iloc[index]
    prior = frame.iloc[index - limits.range_bars:index]
    range_high, range_low = float(prior["high"].max()), float(prior["low"].min())
    spread = max(float(row["high"] - row["low"]), 1e-12)
    lower_wick = float(min(row["open"], row["close"]) - row["low"]) / spread
    upper_wick = float(row["high"] - max(row["open"], row["close"])) / spread
    rel_volume = relative_volume(frame, index)
    if row["low"] < range_low and row["close"] > range_low and lower_wick >= 0.25 and rel_volume >= limits.event_volume_floor:
        return "SPRING"
    if row["high"] > range_high and row["close"] < range_high and upper_wick >= 0.25 and rel_volume >= limits.event_volume_floor:
        return "UPTHRUST"
    if row["close"] > range_high and rel_volume >= 1.1:
        return "SOS"
    if row["close"] < range_low and rel_volume >= 1.1:
        return "SOW"
    return "NONE"


def classify_event(frame: pd.DataFrame, limits: Thresholds) -> str:
    index = len(frame) - 1
    current = raw_event(frame, index, limits)
    if current != "NONE":
        return current
    recent = [(i, raw_event(frame, i, limits)) for i in range(max(limits.range_bars, index - 6), index)]
    row, rel_volume = frame.iloc[index], relative_volume(frame, index)
    for event_index, event in reversed(recent):
        event_row = frame.iloc[event_index]
        if event == "SPRING" and row["low"] > event_row["low"] and rel_volume <= limits.test_volume_ceiling:
            return "TEST_AFTER_SPRING"
        if event == "UPTHRUST" and row["high"] < event_row["high"] and rel_volume <= limits.test_volume_ceiling:
            return "TEST_AFTER_UPTHRUST"
    return "NONE"


def evaluate(minutes: pd.DataFrame, exchange: str, symbol: str, limits: Thresholds | None = None,
             now: float | None = None) -> dict:
    thresholds = limits or Thresholds()
    bars_5m, bars_15m, bars_1h = (resample_closed(minutes, rule) for rule in ("5min", "15min", "1h"))
    if len(bars_5m) < 30 or len(bars_15m) < 40 or len(bars_1h) < 30:
        raise ValueError("insufficient_multitimeframe_bars")
    current_time = time.time() if now is None else now
    market_time = pd.Timestamp(bars_5m["close_time"].iloc[-1]).timestamp()
    market_age = max(0.0, current_time - market_time)
    event = classify_event(bars_15m, thresholds)
    index = len(bars_15m) - 1
    prior = bars_15m.iloc[index - thresholds.range_bars:index]
    range_high, range_low = float(prior["high"].max()), float(prior["low"].min())
    current_15m = bars_15m.iloc[-1]
    atr15 = float(atr(bars_15m).iloc[-1])
    width = range_high - range_low
    width_atr = 0.0 if atr15 <= 0 else width / atr15
    range_position = 0.5 if width <= 0 else float((current_15m["close"] - range_low) / width)
    rel_volume = relative_volume(bars_15m, index)
    ema20 = bars_1h["close"].ewm(span=20, adjust=False).mean()
    ema50 = bars_1h["close"].ewm(span=50, adjust=False).mean()
    hourly_context = "BULLISH" if ema20.iloc[-1] > ema50.iloc[-1] else "BEARISH"
    bullish_events = {"SPRING", "TEST_AFTER_SPRING", "SOS"}
    bearish_events = {"UPTHRUST", "TEST_AFTER_UPTHRUST", "SOW"}
    direction = "LONG" if event in bullish_events else "SHORT" if event in bearish_events else "NONE"
    current_5m, previous_5m = bars_5m.iloc[-1], bars_5m.iloc[-2]
    trigger = bool(current_5m["close"] > previous_5m["high"]) if direction == "LONG" else (
        bool(current_5m["close"] < previous_5m["low"]) if direction == "SHORT" else False
    )
    entry = float(current_5m["close"])
    if direction == "LONG":
        stop, target = min(float(current_15m["low"]), range_low) - 0.25 * atr15, range_high
        risk, reward = entry - stop, range_high - entry
    elif direction == "SHORT":
        stop, target = max(float(current_15m["high"]), range_high) + 0.25 * atr15, range_low
        risk, reward = stop - entry, entry - range_low
    else:
        stop = target = entry
        risk = reward = 0.0
    rr = 0.0 if risk <= 0 else max(0.0, reward / risk)
    range_quality = thresholds.min_range_atr <= width_atr <= thresholds.max_range_atr
    context_aligned = (direction == "LONG" and hourly_context == "BULLISH") or (direction == "SHORT" and hourly_context == "BEARISH")
    blockers = []
    if market_age > thresholds.max_market_age_seconds: blockers.append("stale_market_data")
    if not range_quality: blockers.append("range_quality_outside_bounds")
    if event == "NONE": blockers.append("no_wyckoff_event")
    if event != "NONE" and not trigger: blockers.append("trigger_not_confirmed")
    if event != "NONE" and rr + 1e-9 < thresholds.min_rr: blockers.append("rr_below_floor")
    if event != "NONE" and not context_aligned: blockers.append("hourly_context_not_aligned")
    score = int(round(20 * range_quality + 30 * (event != "NONE") + 15 * min(rel_volume / 1.5, 1.0)
                      + 15 * trigger + 10 * context_aligned + 10 * min(rr / 3.0, 1.0)))
    hard_reject = any(item in blockers for item in ("stale_market_data", "range_quality_outside_bounds"))
    if not blockers and score >= thresholds.ready_score:
        decision, status, reason = "ALLOW", "READY", "wyckoff_shadow_gate_passed"
    elif hard_reject:
        decision, status, reason = "REJECT", "REJECTED", blockers[0]
    else:
        decision, status, reason = "WAIT", "WATCH", blockers[0] if blockers else "score_below_ready"
    bar_id = pd.Timestamp(current_5m["open_time"]).isoformat()
    signal_id = hashlib.sha256(f"wyckoff_shadow_v1:{exchange}:{symbol}:{bar_id}".encode()).hexdigest()
    phase = "ACCUMULATION_CANDIDATE" if range_position <= 0.5 else "DISTRIBUTION_CANDIDATE"
    features = {
        "rangeHigh": range_high, "rangeLow": range_low, "rangePosition": range_position,
        "rangeWidthATR": width_atr, "atr15m": atr15, "relativeVolume15m": rel_volume,
        "hourlyEMA20": float(ema20.iloc[-1]), "hourlyEMA50": float(ema50.iloc[-1]),
        "hourlyContext": hourly_context, "triggerConfirmed": trigger, "contextAligned": context_aligned,
    }
    return {
        "signalId": signal_id, "strategy": "wyckoff_shadow_v1", "mode": "SHADOW_ONLY", "liveTrading": False,
        "exchange": exchange, "symbol": symbol, "timeframe": "5m/15m/1h", "barId": bar_id,
        "marketTime": datetime.fromtimestamp(market_time, UTC).isoformat(), "marketAgeSeconds": market_age,
        "phaseCandidate": phase, "eventCandidate": event, "direction": direction, "score": score,
        "entry": entry, "stop": stop, "target": target, "rr": rr, "features": features,
        "finalAuthorityStatus": status, "authorityDecision": decision, "authorityReason": reason,
        "authorityTrace": {"blockers": blockers, "thresholds": asdict(thresholds)},
        "executionActionable": False, "executionGatePassed": False,
    }


class SignalJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS signals(
            signal_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, market_time TEXT NOT NULL,
            exchange_name TEXT NOT NULL, symbol TEXT NOT NULL, strategy_version TEXT NOT NULL,
            bar_id TEXT NOT NULL, phase TEXT NOT NULL, event TEXT NOT NULL, direction TEXT NOT NULL,
            score INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
            entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL, rr REAL NOT NULL,
            execution_actionable INTEGER NOT NULL CHECK(execution_actionable=0),
            execution_gate_passed INTEGER NOT NULL CHECK(execution_gate_passed=0), features_json TEXT NOT NULL)""")
        self.connection.commit()

    def record(self, signal: dict) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal["signalId"], time.time(), signal["marketTime"], signal["exchange"], signal["symbol"],
                 signal["strategy"], signal["barId"], signal["phaseCandidate"], signal["eventCandidate"],
                 signal["direction"], signal["score"], signal["authorityDecision"], signal["authorityReason"],
                 signal["entry"], signal["stop"], signal["target"], signal["rr"], 0, 0,
                 json.dumps(signal["features"], separators=(",", ":"))))
        return cursor.rowcount == 1

    def count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM signals").fetchone()[0])


class WyckoffShadowAdapter:
    def __init__(self, silver_root: Path, output_root: Path, exchange: str, symbols: tuple[str, ...], thresholds: Thresholds):
        self.silver_root, self.output_root = silver_root, output_root
        self.exchange, self.symbols, self.thresholds = exchange, symbols, thresholds
        self.journal = SignalJournal(output_root / "signals.db")

    def run_once(self) -> dict:
        signals, inserted = [], 0
        for symbol in self.symbols:
            signal = evaluate(load_closed_minutes(self.silver_root, self.exchange, symbol), self.exchange, symbol, self.thresholds)
            signals.append(signal)
            inserted += int(self.journal.record(signal))
        state = {
            "mode": "SHADOW_ONLY", "liveTrading": False, "adapterVersion": "wyckoff_shadow_v1",
            "updatedAt": datetime.now(UTC).isoformat(), "journalRows": self.journal.count(),
            "newSignals": inserted, "signals": signals,
        }
        self.output_root.mkdir(parents=True, exist_ok=True)
        temporary = self.output_root / "latest-signals.json.tmp"
        temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.output_root / "latest-signals.json")
        os.chmod(self.output_root / "latest-signals.json", 0o640)
        os.chmod(self.output_root / "signals.db", 0o600)
        return state
