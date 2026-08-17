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
    high_volume_ratio: float = 1.5
    low_volume_ratio: float = 0.75
    wide_spread_ratio: float = 1.35
    narrow_spread_ratio: float = 0.75


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


def candle_features(frame: pd.DataFrame, index: int, period: int = 20) -> dict:
    row = frame.iloc[index]
    spread = max(float(row["high"] - row["low"]), 1e-12)
    prior_spreads = (frame["high"] - frame["low"]).iloc[max(0, index - period):index]
    median_spread = float(prior_spreads.median())
    spread_ratio = 0.0 if not math.isfinite(median_spread) or median_spread <= 0 else spread / median_spread
    close_location = float((row["close"] - row["low"]) / spread)
    return {
        "relativeVolume": relative_volume(frame, index, period), "spreadRatio": spread_ratio,
        "closeLocation": max(0.0, min(1.0, close_location)),
        "isUpBar": bool(row["close"] > row["open"]), "isDownBar": bool(row["close"] < row["open"]),
    }


def detect_vsa_events(frame: pd.DataFrame, limits: Thresholds) -> list[dict]:
    """Return deterministic, explainable VSA observations for the latest closed bar."""
    index = len(frame) - 1
    if index < max(limits.range_bars, 2):
        return []
    current, previous = candle_features(frame, index), candle_features(frame, index - 1)
    row, prior_row = frame.iloc[index], frame.iloc[index - 1]
    prior = frame.iloc[index - limits.range_bars:index]
    range_high, range_low = float(prior["high"].max()), float(prior["low"].min())
    events: list[dict] = []

    def add(name: str, direction: str, score: float, explanation: str):
        events.append({"event": name, "direction": direction, "score": int(round(max(0, min(100, score)))),
                       "status": "CONFIRMED", "explanation": explanation})

    rv, sr, cl = current["relativeVolume"], current["spreadRatio"], current["closeLocation"]
    if current["isUpBar"] and rv <= limits.low_volume_ratio and sr <= limits.narrow_spread_ratio and cl < 0.7:
        add("NO_DEMAND", "BEARISH", 55 + 20 * (limits.low_volume_ratio - rv), "Up bar on narrow spread and low relative volume")
    if current["isDownBar"] and rv <= limits.low_volume_ratio and sr <= limits.narrow_spread_ratio and cl > 0.3:
        add("NO_SUPPLY", "BULLISH", 55 + 20 * (limits.low_volume_ratio - rv), "Down bar on narrow spread and low relative volume")
    if rv >= limits.high_volume_ratio and sr >= limits.wide_spread_ratio and cl >= 0.65:
        add("SIGN_OF_STRENGTH", "BULLISH", 60 + 15 * min(rv - 1, 2), "Wide spread closes high on elevated volume")
    if rv >= limits.high_volume_ratio and sr >= limits.wide_spread_ratio and cl <= 0.35:
        add("SIGN_OF_WEAKNESS", "BEARISH", 60 + 15 * min(rv - 1, 2), "Wide spread closes low on elevated volume")
    if rv >= limits.high_volume_ratio and cl >= 0.55 and row["low"] <= prior["low"].quantile(0.15):
        add("STOPPING_VOLUME", "BULLISH", 60 + 15 * min(rv - 1, 2), "High volume rejection near the lower trading-range boundary")
    if row["low"] < range_low and row["close"] > range_low and cl >= 0.55:
        add("SHAKEOUT", "BULLISH", 65 + 10 * min(rv, 2), "Price breaks below range support and closes back inside")
    if row["high"] > range_high and row["close"] < range_high and cl <= 0.45:
        add("UPTHRUST", "BEARISH", 65 + 10 * min(rv, 2), "Price breaks above range resistance and closes back inside")
    if (previous["relativeVolume"] >= limits.high_volume_ratio and rv <= limits.low_volume_ratio
            and row["low"] > prior_row["low"] and cl >= 0.5):
        add("TEST", "BULLISH", 65 + 15 * (limits.low_volume_ratio - rv), "Low-volume retest holds above the prior high-volume low")
    return sorted(events, key=lambda item: (-item["score"], item["event"]))


def classify_market_phase(range_position: float, close: float, range_low: float, range_high: float,
                          hourly_context: str, ema_slope: float, event_names: set[str]) -> tuple[str, int, str]:
    if close > range_high or (hourly_context == "BULLISH" and ema_slope > 0 and range_position >= 0.65):
        return "MARKUP", 78 if close > range_high else 68, "Price is above/near range resistance with bullish higher-timeframe structure"
    if close < range_low or (hourly_context == "BEARISH" and ema_slope < 0 and range_position <= 0.35):
        return "MARKDOWN", 78 if close < range_low else 68, "Price is below/near range support with bearish higher-timeframe structure"
    bullish = bool(event_names & {"NO_SUPPLY", "STOPPING_VOLUME", "TEST", "SHAKEOUT", "SIGN_OF_STRENGTH"})
    bearish = bool(event_names & {"NO_DEMAND", "UPTHRUST", "SIGN_OF_WEAKNESS"})
    if range_position <= 0.55 or bullish:
        return "ACCUMULATION", 65 if bullish else 52, "Price is inside the lower range with accumulation evidence" if bullish else "Price is rotating in the lower half of the trading range"
    return "DISTRIBUTION", 65 if bearish else 52, "Price is inside the upper range with distribution evidence" if bearish else "Price is rotating in the upper half of the trading range"


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
    ema_slope = float(ema20.iloc[-1] - ema20.iloc[-4])
    vsa_events = detect_vsa_events(bars_15m, thresholds)
    event_names = {item["event"] for item in vsa_events}
    phase, phase_score, phase_explanation = classify_market_phase(
        range_position, float(current_15m["close"]), range_low, range_high, hourly_context, ema_slope, event_names)
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
    signal_id = hashlib.sha256(f"wyckoff_vsa_research_v1:{exchange}:{symbol}:{bar_id}".encode()).hexdigest()
    bullish_vsa = sum(item["score"] for item in vsa_events if item["direction"] == "BULLISH")
    bearish_vsa = sum(item["score"] for item in vsa_events if item["direction"] == "BEARISH")
    if bullish_vsa > bearish_vsa: composite_intent = "ACCUMULATING_OR_MARKING_UP"
    elif bearish_vsa > bullish_vsa: composite_intent = "DISTRIBUTING_OR_MARKING_DOWN"
    else: composite_intent = "NEUTRAL_OR_UNCONFIRMED"
    market_story = f"{phase}: {phase_explanation}. " + (
        f"Primary VSA evidence is {vsa_events[0]['event']} ({vsa_events[0]['score']}/100)."
        if vsa_events else "No confirmed VSA event on the latest closed 15m candle.")
    features = {
        "rangeHigh": range_high, "rangeLow": range_low, "rangePosition": range_position,
        "rangeWidthATR": width_atr, "atr15m": atr15, "relativeVolume15m": rel_volume,
        "hourlyEMA20": float(ema20.iloc[-1]), "hourlyEMA50": float(ema50.iloc[-1]),
        "hourlyContext": hourly_context, "hourlyEMA20Slope": ema_slope,
        "triggerConfirmed": trigger, "contextAligned": context_aligned,
        "spreadRatio15m": candle_features(bars_15m, index)["spreadRatio"],
        "closeLocation15m": candle_features(bars_15m, index)["closeLocation"],
    }
    return {
        "signalId": signal_id, "strategy": "wyckoff_vsa_research_v1", "mode": "SHADOW_ONLY", "liveTrading": False,
        "exchange": exchange, "symbol": symbol, "timeframe": "5m/15m/1h", "barId": bar_id,
        "marketTime": datetime.fromtimestamp(market_time, UTC).isoformat(), "marketAgeSeconds": market_age,
        "phaseCandidate": phase, "phaseConfidence": phase_score, "phaseExplanation": phase_explanation,
        "eventCandidate": event, "vsaEvents": vsa_events, "compositeOperatorIntent": composite_intent,
        "marketStory": market_story, "direction": direction, "score": score,
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
        self.connection.execute("""CREATE TABLE IF NOT EXISTS research_observations(
            signal_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, market_time TEXT NOT NULL,
            exchange_name TEXT NOT NULL, symbol TEXT NOT NULL, strategy_version TEXT NOT NULL,
            bar_id TEXT NOT NULL, phase TEXT NOT NULL, phase_confidence INTEGER NOT NULL,
            composite_intent TEXT NOT NULL, vsa_events_json TEXT NOT NULL, market_story TEXT NOT NULL,
            decision TEXT NOT NULL, reason TEXT NOT NULL,
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
            self.connection.execute(
                """INSERT OR IGNORE INTO research_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal["signalId"], time.time(), signal["marketTime"], signal["exchange"], signal["symbol"],
                 signal["strategy"], signal["barId"], signal["phaseCandidate"], signal["phaseConfidence"],
                 signal["compositeOperatorIntent"], json.dumps(signal["vsaEvents"], separators=(",", ":")),
                 signal["marketStory"], signal["authorityDecision"], signal["authorityReason"], 0, 0,
                 json.dumps(signal["features"], separators=(",", ":"))))
        return cursor.rowcount == 1

    def count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM signals").fetchone()[0])

    def research_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM research_observations").fetchone()[0])

    def last_market_time(self, symbol: str) -> pd.Timestamp | None:
        row = self.connection.execute(
            "SELECT max(market_time) FROM research_observations WHERE symbol=?", (symbol,)
        ).fetchone()
        return None if not row or not row[0] else pd.Timestamp(row[0])


class WyckoffShadowAdapter:
    def __init__(self, silver_root: Path, output_root: Path, exchange: str, symbols: tuple[str, ...],
                 thresholds: Thresholds, max_catchup_bars: int = 96):
        self.silver_root, self.output_root = silver_root, output_root
        self.exchange, self.symbols, self.thresholds = exchange, symbols, thresholds
        self.max_catchup_bars = max(1, max_catchup_bars)
        self.journal = SignalJournal(output_root / "signals.db")

    def run_once(self) -> dict:
        signals, inserted, scheduler_state = [], 0, []
        for symbol in self.symbols:
            try:
                minutes = load_closed_minutes(self.silver_root, self.exchange, symbol)
            except (FileNotFoundError, OSError, ValueError):
                scheduler_state.append({
                    "symbol": symbol, "status": "WARMING_UP", "reason": "source_not_available",
                    "processedBars": 0, "backlogBars": 0, "missingResearchBars": 0,
                    "processingLagSeconds": 0.0, "watermark": None,
                    "availableMinuteBars": 0, "requiredMinuteBars": 1800,
                })
                continue
            closed_5m = resample_closed(minutes, "5min")
            if closed_5m.empty or len(minutes) < 1800:
                scheduler_state.append({
                    "symbol": symbol, "status": "WARMING_UP", "reason": "insufficient_closed_history",
                    "processedBars": 0, "backlogBars": 0, "missingResearchBars": 0,
                    "processingLagSeconds": 0.0, "watermark": None,
                    "availableMinuteBars": len(minutes), "requiredMinuteBars": 1800,
                })
                continue
            watermark = self.journal.last_market_time(symbol)
            available = closed_5m if watermark is None else closed_5m[
                pd.to_datetime(closed_5m["close_time"], utc=True) > watermark]
            # A new installation starts at the latest closed bar. Historical replay is an explicit job,
            # never an accidental side effect of deploying the live shadow scheduler.
            if watermark is None:
                available = closed_5m.tail(1)
            backlog_before = len(available)
            processed = 0
            latest_signal = None
            for candidate in available.head(self.max_catchup_bars).itertuples(index=False):
                candidate_close = pd.Timestamp(candidate.close_time)
                event_minutes = minutes[pd.to_datetime(minutes["close_time"], utc=True) <= candidate_close]
                if len(event_minutes) < 1800:
                    continue
                signal = evaluate(event_minutes, self.exchange, symbol, self.thresholds,
                                  now=candidate_close.timestamp())
                inserted += int(self.journal.record(signal)); processed += 1; latest_signal = signal
            # Projection always describes the latest available market, while journal writes above are
            # ordered by closed-bar event time and cannot see a future candle.
            projection = evaluate(minutes, self.exchange, symbol, self.thresholds)
            if backlog_before <= self.max_catchup_bars and (
                    latest_signal is None or latest_signal["signalId"] != projection["signalId"]):
                inserted += int(self.journal.record(projection))
            signals.append(projection)
            latest_close = pd.Timestamp(closed_5m["close_time"].iloc[-1])
            current_watermark = self.journal.last_market_time(symbol)
            expected_steps = 0 if current_watermark is None else max(
                0, int((latest_close - current_watermark).total_seconds() // 300))
            scheduler_state.append({
                "symbol": symbol, "status": "READY", "reason": "ready", "processedBars": processed,
                "backlogBars": max(0, backlog_before - processed),
                "missingResearchBars": expected_steps,
                "processingLagSeconds": max(0.0, time.time() - latest_close.timestamp()),
                "watermark": None if current_watermark is None else current_watermark.isoformat(),
                "availableMinuteBars": len(minutes), "requiredMinuteBars": 1800,
            })
        state = {
            "mode": "SHADOW_ONLY", "liveTrading": False, "executionActionable": False,
            "executionGatePassed": False, "adapterVersion": "wyckoff_vsa_research_v1",
            "updatedAt": datetime.now(UTC).isoformat(), "journalRows": self.journal.count(),
            "researchJournalRows": self.journal.research_count(), "newSignals": inserted,
            "scheduler": scheduler_state, "signals": signals,
        }
        self.output_root.mkdir(parents=True, exist_ok=True)
        temporary = self.output_root / "latest-signals.json.tmp"
        temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temporary, self.output_root / "latest-signals.json")
        os.chmod(self.output_root / "latest-signals.json", 0o640)
        os.chmod(self.output_root / "signals.db", 0o600)
        return state
