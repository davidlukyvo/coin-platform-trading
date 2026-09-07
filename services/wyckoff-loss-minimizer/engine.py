from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


VERSION = "wyckoff_loss_minimizer_v1"


@dataclass(frozen=True)
class ResearchConfig:
    symbol: str = "BTCUSDT"
    notional: float = 200.0
    fee_bps: float = 5.0
    tax_bps: float = 10.0
    slippage_bps: float = 2.0
    max_holding_bars: int = 1440
    min_train_closed: int = 15
    min_validation_closed: int = 10
    min_oos_closed: int = 10
    max_oos_drawdown: float = 5.0


def load_signals(path: Path, symbol: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    query = """SELECT signal_id,market_time,phase,event,direction,score,entry,stop,target,rr,features_json
               FROM signals WHERE symbol=? AND decision='ALLOW'
               AND direction IN ('LONG','SHORT') ORDER BY market_time"""
    frame = pd.read_sql_query(query, con, params=(symbol,))
    con.close()
    if frame.empty:
        return frame
    frame["market_time"] = pd.to_datetime(frame.market_time, utc=True)
    features = frame.features_json.map(json.loads).apply(pd.Series)
    return pd.concat([frame.drop(columns="features_json"), features], axis=1)


def load_bars(root: Path, symbol: str) -> pd.DataFrame:
    pattern = str(root / "binance/spot/kline_1m" / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """SELECT open_time,high,low,close FROM read_parquet(?) WHERE is_closed
               QUALIFY row_number() OVER(PARTITION BY open_time ORDER BY exchange_event_time DESC)=1
               ORDER BY open_time"""
    frame = duckdb.connect().execute(query, [pattern]).df()
    frame["open_time"] = pd.to_datetime(frame.open_time, utc=True)
    for column in ("high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column])
    return frame


def outcomes(signals: pd.DataFrame, bars: pd.DataFrame, config: ResearchConfig) -> pd.DataFrame:
    rows = []
    for signal in signals.itertuples(index=False):
        future = bars[bars.open_time > signal.market_time].head(config.max_holding_bars)
        side = signal.direction
        entry = float(signal.entry) * (1 + (config.slippage_bps if side == "LONG" else -config.slippage_bps) / 10000)
        quantity = config.notional / entry
        exit_price = None
        exit_time = pd.NaT
        exit_reason = "UNRESOLVED"
        for bar in future.itertuples(index=False):
            stop_hit = bar.low <= signal.stop if side == "LONG" else bar.high >= signal.stop
            target_hit = bar.high >= signal.target if side == "LONG" else bar.low <= signal.target
            if stop_hit:
                exit_price, exit_time, exit_reason = float(signal.stop), bar.open_time, "STOP"
                break
            if target_hit:
                exit_price, exit_time, exit_reason = float(signal.target), bar.open_time, "TARGET"
                break
        row = dict(zip(signals.columns, signal))
        if exit_price is None:
            row.update(resolved=False, exit_time=exit_time, exit_reason=exit_reason,
                       gross_pnl=np.nan, fees=np.nan, taxes=np.nan, net_pnl=np.nan)
        else:
            exit_price *= 1 + (-config.slippage_bps if side == "LONG" else config.slippage_bps) / 10000
            exit_notional = quantity * exit_price
            gross = quantity * (exit_price - entry) * (1 if side == "LONG" else -1)
            fees = (config.notional + exit_notional) * config.fee_bps / 10000
            taxes = (exit_notional if side == "LONG" else config.notional) * config.tax_bps / 10000
            row.update(resolved=True, exit_time=exit_time, exit_reason=exit_reason,
                       gross_pnl=gross, fees=fees, taxes=taxes, net_pnl=gross-fees-taxes)
        rows.append(row)
    return pd.DataFrame(rows)


def stats(frame: pd.DataFrame) -> dict:
    selected = []
    busy_until = pd.Timestamp.min.tz_localize("UTC")
    for row in frame.sort_values("market_time").itertuples(index=False):
        if row.market_time <= busy_until:
            continue
        selected.append(row)
        if row.resolved:
            busy_until = row.exit_time
    closed = [row for row in selected if row.resolved]
    pnl = [float(row.net_pnl) for row in closed]
    curve = np.cumsum([0.0] + pnl)
    drawdown = np.maximum.accumulate(curve) - curve
    return {"eligible": len(frame), "taken": len(selected), "closed": len(closed),
            "wins": sum(value > 0 for value in pnl), "losses": sum(value <= 0 for value in pnl),
            "unresolved": sum(not row.resolved for row in selected),
            "grossPnl": round(sum(float(row.gross_pnl) for row in closed), 6),
            "fees": round(sum(float(row.fees) for row in closed), 6),
            "taxes": round(sum(float(row.taxes) for row in closed), 6),
            "netPnl": round(sum(pnl), 6), "maxDrawdown": round(float(drawdown.max()), 6),
            "winRate": round(sum(value > 0 for value in pnl) / len(pnl), 6) if pnl else 0.0}


def candidate_masks(frame: pd.DataFrame) -> dict[str, pd.Series]:
    masks = {"all": pd.Series(True, index=frame.index)}
    masks["short_only"] = frame.direction == "SHORT"
    masks["long_only"] = frame.direction == "LONG"
    for event in sorted(frame.event.dropna().unique()):
        masks[f"event={event}"] = frame.event == event
    for threshold in (70, 75, 80, 85, 90):
        masks[f"score>={threshold}"] = frame.score >= threshold
    for threshold in (2, 2.5, 3, 4):
        masks[f"rr>={threshold}"] = frame.rr >= threshold
    for threshold in (1, 1.25, 1.5):
        masks[f"relativeVolume15m>={threshold}"] = frame.relativeVolume15m >= threshold
    for threshold in (1, 1.2, 1.35):
        masks[f"spreadRatio15m>={threshold}"] = frame.spreadRatio15m >= threshold
    masks["hourly_trend_aligned"] = (((frame.direction == "LONG") & (frame.hourlyContext == "BULLISH")) |
                                      ((frame.direction == "SHORT") & (frame.hourlyContext == "BEARISH")))
    masks["hourly_slope_aligned"] = (((frame.direction == "LONG") & (frame.hourlyEMA20Slope > 0)) |
                                      ((frame.direction == "SHORT") & (frame.hourlyEMA20Slope < 0)))
    masks["context_aligned"] = frame.contextAligned == True
    return masks


def analyze(signals: pd.DataFrame, bars: pd.DataFrame, config: ResearchConfig) -> dict:
    trades = outcomes(signals, bars, config)
    count = len(trades)
    train_end, validation_end = int(count * .6), int(count * .8)
    split = {"train": np.arange(count) < train_end,
             "validation": (np.arange(count) >= train_end) & (np.arange(count) < validation_end),
             "outOfSample": np.arange(count) >= validation_end}
    atoms = candidate_masks(trades)
    candidates = dict(atoms)
    atom_rank = sorted((name for name in atoms if name != "all"),
                       key=lambda name: stats(trades[split["train"] & atoms[name]])["netPnl"], reverse=True)[:15]
    for index, left in enumerate(atom_rank):
        for right in atom_rank[index+1:]:
            candidates[f"{left} AND {right}"] = atoms[left] & atoms[right]
    evaluations = []
    for name, mask in candidates.items():
        item = {"gate": name, **{segment: stats(trades[mask & segment_mask]) for segment, segment_mask in split.items()}}
        item["qualified"] = (item["train"]["closed"] >= config.min_train_closed and
                             item["validation"]["closed"] >= config.min_validation_closed and
                             item["outOfSample"]["closed"] >= config.min_oos_closed and
                             all(item[part]["netPnl"] > 0 for part in ("train", "validation", "outOfSample")) and
                             item["outOfSample"]["maxDrawdown"] <= config.max_oos_drawdown)
        evaluations.append(item)
    qualified = [item for item in evaluations if item["qualified"]]
    ranked = sorted(evaluations, key=lambda item: (item["validation"]["netPnl"], item["train"]["netPnl"]), reverse=True)
    last_signal = str(signals.signal_id.iloc[-1]) if not signals.empty else "none"
    report_id = hashlib.sha256(f"{VERSION}:{last_signal}:{config}".encode()).hexdigest()
    return {"reportId": report_id, "mode": "SHADOW_RESEARCH_ONLY", "liveTrading": False,
            "executionActionable": False, "executionGatePassed": False, "version": VERSION,
            "updatedAt": datetime.now(UTC).isoformat(), "symbol": config.symbol,
            "sourceSignals": count, "lastSignalId": last_signal,
            "method": {"split": "chronological_60_20_20", "positionPolicy": "non_overlapping",
                       "sameBarPolicy": "STOP_FIRST_CONSERVATIVE", "funding": "NOT_INSTRUMENTED",
                       "selection": "train_validation_only_then_reveal_oos"},
            "costs": {"notional": config.notional, "feeBps": config.fee_bps,
                      "taxBps": config.tax_bps, "slippageBps": config.slippage_bps},
            "baseline": {name: stats(trades[mask]) for name, mask in split.items()},
            "qualifiedCandidates": qualified, "bestResearchCandidates": ranked[:10],
            "recommendation": "NO_TRADE_RESEARCH_LOCK" if not qualified else "QUALIFIED_SHADOW_CANDIDATE"}


def persist(report: dict, output: Path) -> bool:
    output.mkdir(parents=True, exist_ok=True)
    latest = output / "latest.json"
    previous = json.loads(latest.read_text()) if latest.exists() else None
    changed = not previous or previous.get("reportId") != report["reportId"]
    if changed:
        with (output / "reports.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, separators=(",", ":")) + "\n")
    temporary = output / "latest.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(latest)
    return changed
