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
    min_minute_bars: int = 300
    pivot_left: int = 3
    pivot_right: int = 3
    sequence_max_bars: int = 48
    ote_max_bars: int = 36
    max_market_age_seconds: int = 3900
    divergence_lookback: int = 12
    divergence_atr_fraction: float = 0.20


def load_closed_minutes(root: Path, exchange: str, symbol: str, limit: int = 2600) -> pd.DataFrame:
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


def resample_5m(minutes: pd.DataFrame) -> pd.DataFrame:
    source = minutes.copy()
    source["open_time"] = pd.to_datetime(source["open_time"], utc=True)
    source["close_time"] = pd.to_datetime(source["close_time"], utc=True)
    bars = source.set_index("open_time").resample("5min", label="left", closed="left", origin="epoch").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), close_time=("close_time", "max"), minute_bars=("close", "count"),
    ).dropna(subset=["open", "high", "low", "close"])
    return bars[bars["minute_bars"] == 5].reset_index()


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous = frame["close"].shift(1)
    ranges = pd.concat([frame["high"] - frame["low"], (frame["high"] - previous).abs(),
                        (frame["low"] - previous).abs()], axis=1).max(axis=1)
    return ranges.rolling(period).mean()


def is_pivot(frame: pd.DataFrame, index: int, left: int, right: int, high: bool) -> bool:
    if index < left or index + right >= len(frame):
        return False
    column = "high" if high else "low"
    value = float(frame[column].iloc[index])
    neighbors = frame[column].iloc[index - left:index + right + 1].drop(frame.index[index])
    return value > float(neighbors.max()) if high else value < float(neighbors.min())


class Journal:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        self.path = root / "liquidity-structure.db"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
              CREATE TABLE IF NOT EXISTS observations(
                observation_id TEXT PRIMARY KEY, market_time TEXT NOT NULL, symbol TEXT NOT NULL,
                evidence_json TEXT NOT NULL, created_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS symbol_state(
                symbol TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at REAL NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def load_state(self, symbol: str) -> dict:
        row = self.db.execute("SELECT state_json FROM symbol_state WHERE symbol=?", (symbol,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, symbol: str, state: dict, observation: dict) -> bool:
        payload = json.dumps(observation, separators=(",", ":"), sort_keys=True)
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                (observation["observationId"], observation["marketTime"], symbol, payload, time.time()),
            )
            self.db.execute("INSERT OR REPLACE INTO symbol_state VALUES(?,?,?)",
                            (symbol, json.dumps(state, separators=(",", ":")), time.time()))
        return cursor.rowcount == 1

    def latest(self, symbol: str) -> dict | None:
        row = self.db.execute("SELECT evidence_json FROM observations WHERE symbol=? ORDER BY market_time DESC LIMIT 1",
                              (symbol,)).fetchone()
        return json.loads(row[0]) if row else None

    def last_market_time(self, symbol: str) -> pd.Timestamp | None:
        row = self.db.execute("SELECT max(market_time) FROM observations WHERE symbol=?", (symbol,)).fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def rows(self) -> int:
        return int(self.db.execute("SELECT count(*) FROM observations").fetchone()[0])


def divergence(primary: pd.DataFrame, peers: dict[str, pd.DataFrame], index: int, limits: Thresholds) -> list[dict]:
    if index < limits.divergence_lookback + 1:
        return []
    result = []
    p = primary.iloc[index]
    prior = primary.iloc[index - limits.divergence_lookback:index]
    p_atr = float(atr(primary).iloc[index])
    if not math.isfinite(p_atr) or p_atr <= 0:
        return []
    timestamp = pd.Timestamp(p["open_time"])
    for symbol, frame in peers.items():
        matches = frame.index[frame["open_time"] == timestamp]
        if len(matches) != 1:
            continue
        peer_index = int(matches[0])
        if peer_index < limits.divergence_lookback:
            continue
        q, q_prior = frame.iloc[peer_index], frame.iloc[peer_index - limits.divergence_lookback:peer_index]
        primary_hh, primary_ll = p["high"] > prior["high"].max(), p["low"] < prior["low"].min()
        peer_hh, peer_ll = q["high"] > q_prior["high"].max(), q["low"] < q_prior["low"].min()
        if primary_hh and not peer_hh:
            result.append({"peer": symbol, "direction": "BEARISH", "event": "SMT_HIGH_DIVERGENCE",
                           "normalizedMagnitude": float((p["high"] - prior["high"].max()) / p_atr)})
        if primary_ll and not peer_ll:
            result.append({"peer": symbol, "direction": "BULLISH", "event": "SMT_LOW_DIVERGENCE",
                           "normalizedMagnitude": float((prior["low"].min() - p["low"]) / p_atr)})
    return [item for item in result if item["normalizedMagnitude"] >= limits.divergence_atr_fraction]


def evaluate_bar(frame: pd.DataFrame, index: int, symbol: str, exchange: str, state: dict,
                 peers: dict[str, pd.DataFrame], limits: Thresholds, now: float | None = None) -> tuple[dict, dict]:
    row = frame.iloc[index]
    confirmed = index - limits.pivot_right
    if is_pivot(frame, confirmed, limits.pivot_left, limits.pivot_right, True):
        state["bsl"] = float(frame["high"].iloc[confirmed])
        state["bslTime"] = pd.Timestamp(frame["open_time"].iloc[confirmed]).isoformat()
    if is_pivot(frame, confirmed, limits.pivot_left, limits.pivot_right, False):
        state["ssl"] = float(frame["low"].iloc[confirmed])
        state["sslTime"] = pd.Timestamp(frame["open_time"].iloc[confirmed]).isoformat()
    bsl, ssl = state.get("bsl"), state.get("ssl")
    sweep = "NONE"
    if bsl is not None and row["high"] > bsl and row["close"] < bsl:
        sweep = "BSL_SWEEP"; state.update(lastSweep="BSL", sweepTime=pd.Timestamp(row["open_time"]).isoformat(),
                                           sweepExtreme=float(row["high"]))
        state.pop("ote", None)
    elif ssl is not None and row["low"] < ssl and row["close"] > ssl:
        sweep = "SSL_SWEEP"; state.update(lastSweep="SSL", sweepTime=pd.Timestamp(row["open_time"]).isoformat(),
                                           sweepExtreme=float(row["low"]))
        state.pop("ote", None)
    mss = "NONE"
    sweep_age = ((pd.Timestamp(row["open_time"]) - pd.Timestamp(state["sweepTime"])) / pd.Timedelta("5min")
                 if state.get("sweepTime") else 100000)
    prior_close = float(frame["close"].iloc[index - 1]) if index else float(row["close"])
    if 0 < sweep_age <= limits.sequence_max_bars and state.get("lastSweep") == "SSL" and bsl is not None \
            and row["close"] > bsl >= prior_close:
        mss = "BULLISH_MSS"; distance = float(row["close"] - state["sweepExtreme"])
        state["ote"] = {"direction": "LONG", "top": float(row["close"] - distance * .618),
                        "bottom": float(row["close"] - distance * .786),
                        "armedTime": pd.Timestamp(row["open_time"]).isoformat()}
    elif 0 < sweep_age <= limits.sequence_max_bars and state.get("lastSweep") == "BSL" and ssl is not None \
            and row["close"] < ssl <= prior_close:
        mss = "BEARISH_MSS"; distance = float(state["sweepExtreme"] - row["close"])
        state["ote"] = {"direction": "SHORT", "top": float(row["close"] + distance * .786),
                        "bottom": float(row["close"] + distance * .618),
                        "armedTime": pd.Timestamp(row["open_time"]).isoformat()}
    ote = state.get("ote")
    ote_status, decision, direction = "NONE", "WAIT", "NONE"
    if ote:
        age = (pd.Timestamp(row["open_time"]) - pd.Timestamp(ote["armedTime"])) / pd.Timedelta("5min")
        if age > limits.ote_max_bars:
            ote_status = "EXPIRED"; state.pop("ote", None)
        else:
            overlaps = row["low"] <= ote["top"] and row["high"] >= ote["bottom"]
            confirms = row["close"] > row["open"] if ote["direction"] == "LONG" else row["close"] < row["open"]
            ote_status = "CONFIRMED" if age > 0 and overlaps and confirms else "PENDING"
            if ote_status == "CONFIRMED":
                decision, direction = "ALLOW_SHADOW", ote["direction"]
                state.pop("ote", None)
    cross_asset = divergence(frame, peers, index, limits)
    close_time = pd.Timestamp(row["close_time"])
    age_seconds = max(0.0, (time.time() if now is None else now) - close_time.timestamp())
    blockers = []
    if age_seconds > limits.max_market_age_seconds: blockers.append("stale_market_data")
    if decision != "ALLOW_SHADOW": blockers.append("sequence_not_confirmed")
    if blockers and decision == "ALLOW_SHADOW": decision = "REJECT_SHADOW"
    bar_id = pd.Timestamp(row["open_time"]).isoformat()
    observation_id = hashlib.sha256(f"liquidity_structure_v1:{exchange}:{symbol}:{bar_id}".encode()).hexdigest()
    evidence = {
        "observationId": observation_id, "strategy": "liquidity_structure_research_v1", "mode": "SHADOW_ONLY",
        "liveTrading": False, "executionActionable": False, "executionGatePassed": False,
        "exchange": exchange, "symbol": symbol, "timeframe": "5m", "barId": bar_id,
        "marketTime": close_time.isoformat(), "marketAgeSeconds": age_seconds,
        "decision": decision, "direction": direction, "liquiditySweep": sweep, "marketStructureShift": mss,
        "oteStatus": ote_status, "bsl": bsl, "ssl": ssl, "crossAssetDivergence": cross_asset,
        "authorityReason": blockers[0] if blockers else "sweep_mss_ote_confirmed",
        "thresholds": asdict(limits),
    }
    return state, evidence


class LiquidityStructureAdapter:
    def __init__(self, silver_root: Path, output_root: Path, exchange: str, symbols: tuple[str, ...], limits: Thresholds):
        self.silver_root, self.output_root, self.exchange = silver_root, output_root, exchange
        self.symbols, self.limits = symbols, limits
        self.journal = Journal(output_root)

    def run_once(self) -> dict:
        minute_frames = {symbol: load_closed_minutes(self.silver_root, self.exchange, symbol) for symbol in self.symbols}
        bars = {symbol: resample_5m(frame) for symbol, frame in minute_frames.items()}
        scheduler, latest = [], []
        for symbol, frame in bars.items():
            if len(minute_frames[symbol]) < self.limits.min_minute_bars or len(frame) < 30:
                scheduler.append({"symbol": symbol, "status": "WARMING_UP", "processedBars": 0,
                                  "availableMinuteBars": len(minute_frames[symbol])})
                continue
            state = self.journal.load_state(symbol)
            last_time = self.journal.last_market_time(symbol)
            candidates = frame.index if last_time is None else frame.index[frame["close_time"] > last_time]
            processed = 0
            start = max(20, int(candidates[0]) if len(candidates) else len(frame))
            if last_time is None:
                start = max(20, len(frame) - 100)
            for index in range(start, len(frame)):
                peer_frames = {name: peer for name, peer in bars.items() if name != symbol}
                state, observation = evaluate_bar(frame, index, symbol, self.exchange, state, peer_frames, self.limits)
                processed += int(self.journal.save(symbol, state, observation))
            item = self.journal.latest(symbol)
            if item: latest.append(item)
            scheduler.append({"symbol": symbol, "status": "READY", "processedBars": processed,
                              "availableMinuteBars": len(minute_frames[symbol])})
        projection = {"mode": "SHADOW_ONLY", "liveTrading": False, "executionActionable": False,
                      "executionGatePassed": False, "adapterVersion": "liquidity_structure_research_v1",
                      "updatedAt": datetime.now(UTC).isoformat(), "signals": latest, "scheduler": scheduler,
                      "journalRows": self.journal.rows()}
        temporary = self.output_root / "latest-signals.json.tmp"
        temporary.write_text(json.dumps(projection, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.output_root / "latest-signals.json")
        os.chmod(self.output_root / "latest-signals.json", 0o600)
        return projection
