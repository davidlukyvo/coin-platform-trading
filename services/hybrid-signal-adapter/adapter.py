from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd


@dataclass(frozen=True)
class Thresholds:
    min_bars: int = 60
    max_market_age_seconds: int = 3900
    min_relative_volume: float = 1.0
    min_rr: float = 2.0


def load_closed_bars(root: Path, exchange: str, symbol: str, limit: int = 240) -> pd.DataFrame:
    stream = "kline_1m" if exchange == "binance" else "kline_1min"
    pattern = str(root / exchange / "spot" / stream / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """SELECT open_time,close_time,open,high,low,close,volume
               FROM read_parquet(?) WHERE is_closed
               QUALIFY row_number() OVER (PARTITION BY open_time ORDER BY exchange_event_time DESC)=1
               ORDER BY open_time DESC LIMIT ?"""
    frame = duckdb.connect().execute(query, [pattern, limit]).df().sort_values("open_time").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column])
    return frame


def evaluate(frame: pd.DataFrame, exchange: str, symbol: str, thresholds: Thresholds | None = None,
             now: float | None = None) -> dict:
    limits = thresholds or Thresholds()
    if len(frame) < limits.min_bars:
        raise ValueError("insufficient_bars")
    current_time = time.time() if now is None else now
    close_time = pd.Timestamp(frame["close_time"].iloc[-1]).timestamp()
    age = max(0.0, current_time - close_time)
    close = frame["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    previous = close.shift(1)
    true_range = pd.concat(
        [(frame["high"] - frame["low"]), (frame["high"] - previous).abs(), (frame["low"] - previous).abs()], axis=1
    ).max(axis=1)
    atr = float(true_range.rolling(14).mean().iloc[-1])
    average_volume = float(frame["volume"].shift(1).rolling(20).mean().iloc[-1])
    relative_volume = 0.0 if average_volume <= 0 else float(frame["volume"].iloc[-1] / average_volume)
    price = float(close.iloc[-1])
    stop = price - 1.5 * atr
    target = price + 3.0 * atr
    risk = price - stop
    rr = 0.0 if risk <= 0 else (target - price) / risk
    trend = bool(ema12.iloc[-1] > ema26.iloc[-1] > ema50.iloc[-1])
    trigger = bool(price > float(frame["high"].iloc[-2]))
    blockers: list[str] = []
    if age > limits.max_market_age_seconds: blockers.append("stale_market_data")
    if not trend: blockers.append("trend_not_aligned")
    if relative_volume < limits.min_relative_volume: blockers.append("relative_volume_below_floor")
    if rr + 1e-9 < limits.min_rr: blockers.append("rr_below_floor")
    if trend and not trigger: blockers.append("trigger_not_confirmed")
    hard_reject = any(item in blockers for item in ("stale_market_data", "trend_not_aligned", "rr_below_floor"))
    if not blockers:
        decision, tier, reason = "ALLOW", "READY", "hybrid_shadow_gate_passed"
    elif not hard_reject:
        decision, tier, reason = "WAIT", "WATCH", blockers[0]
    else:
        decision, tier, reason = "REJECT", "REJECTED", blockers[0]
    score = int(round(35 * trend + 25 * trigger + 20 * min(relative_volume / 2.0, 1.0) + 20 * min(rr / 3.0, 1.0)))
    bar_id = pd.Timestamp(frame["open_time"].iloc[-1]).isoformat()
    signal_id = hashlib.sha256(f"hybrid_shadow_v1:{exchange}:{symbol}:{bar_id}".encode()).hexdigest()
    return {
        "signalId": signal_id, "strategy": "hybrid_shadow_v1", "mode": "SHADOW_ONLY", "liveTrading": False,
        "exchange": exchange, "symbol": symbol, "timeframe": "1m", "barId": bar_id,
        "marketTime": datetime.fromtimestamp(close_time, UTC).isoformat(), "marketAgeSeconds": age,
        "price": price, "entry": price, "stop": stop, "target": target, "rr": rr,
        "features": {"ema12": float(ema12.iloc[-1]), "ema26": float(ema26.iloc[-1]), "ema50": float(ema50.iloc[-1]),
                     "atr14": atr, "relativeVolume20": relative_volume, "trendAligned": trend, "triggerConfirmed": trigger},
        "score": score, "finalAuthorityStatus": tier, "authorityDecision": decision, "authorityReason": reason,
        "authorityTrace": {"blockers": blockers, "thresholds": asdict(limits)},
        "executionActionable": False, "executionGatePassed": False,
    }


class HybridShadowAdapter:
    def __init__(self, silver_root: Path, output_root: Path, exchange: str, symbols: tuple[str, ...], thresholds: Thresholds):
        self.silver_root, self.output_root = silver_root, output_root
        self.exchange, self.symbols, self.thresholds = exchange, symbols, thresholds

    def run_once(self) -> dict:
        signals = [evaluate(load_closed_bars(self.silver_root, self.exchange, symbol), self.exchange, symbol, self.thresholds)
                   for symbol in self.symbols]
        state = {"mode": "SHADOW_ONLY", "liveTrading": False, "sourceContract": "SystemTrader_Hybrid",
                 "adapterVersion": "hybrid_shadow_v1", "updatedAt": datetime.now(UTC).isoformat(), "signals": signals}
        self.output_root.mkdir(parents=True, exist_ok=True)
        temporary = self.output_root / "latest-signals.json.tmp"
        temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.output_root / "latest-signals.json")
        os.chmod(self.output_root / "latest-signals.json", 0o640)
        return state
