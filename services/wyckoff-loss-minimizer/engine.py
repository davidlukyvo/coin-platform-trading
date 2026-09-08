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


VERSION = "wyckoff_loss_minimizer_gate_v2"


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
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = -sum(value for value in pnl if value <= 0)
    profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit else 0.0)
    return {"eligible": len(frame), "taken": len(selected), "closed": len(closed),
            "wins": sum(value > 0 for value in pnl), "losses": sum(value <= 0 for value in pnl),
            "unresolved": sum(not row.resolved for row in selected),
            "grossPnl": round(sum(float(row.gross_pnl) for row in closed), 6),
            "fees": round(sum(float(row.fees) for row in closed), 6),
            "taxes": round(sum(float(row.taxes) for row in closed), 6),
            "netPnl": round(sum(pnl), 6), "maxDrawdown": round(float(drawdown.max()), 6),
            "averageNetPnl": round(sum(pnl) / len(pnl), 6) if pnl else 0.0,
            "profitFactor": round(float(profit_factor), 6),
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
    if "phase" in frame:
        for phase in sorted(frame.phase.dropna().unique()):
            masks[f"phase={phase}"] = frame.phase == phase
    if "relativeVolume15m" in frame:
        for threshold in (1, 1.25, 1.5):
            masks[f"relativeVolume15m>={threshold}"] = frame.relativeVolume15m >= threshold
    if "spreadRatio15m" in frame:
        for lower, upper in ((0.8, 1.2), (1.0, 1.5), (1.2, 2.0)):
            masks[f"spreadRatio15m={lower}-{upper}"] = frame.spreadRatio15m.between(lower, upper)
    if "hourlyContext" in frame:
        masks["hourly_trend_aligned"] = (((frame.direction == "LONG") & (frame.hourlyContext == "BULLISH")) |
                                          ((frame.direction == "SHORT") & (frame.hourlyContext == "BEARISH")))
        masks["hourly_counter_trend"] = (((frame.direction == "LONG") & (frame.hourlyContext == "BEARISH")) |
                                          ((frame.direction == "SHORT") & (frame.hourlyContext == "BULLISH")))
    if "hourlyEMA20Slope" in frame:
        masks["hourly_slope_aligned"] = (((frame.direction == "LONG") & (frame.hourlyEMA20Slope > 0)) |
                                          ((frame.direction == "SHORT") & (frame.hourlyEMA20Slope < 0)))
    if "contextAligned" in frame:
        masks["context_aligned"] = frame.contextAligned == True
    return masks


def diagnostics(frame: pd.DataFrame) -> dict:
    groups = {}
    dimensions = [name for name in ("direction", "event", "phase", "hourlyContext") if name in frame]
    for dimension in dimensions:
        groups[dimension] = {str(value): stats(part) for value, part in frame.groupby(dimension, dropna=False)}
    if "spreadRatio15m" in frame:
        bands = pd.cut(frame.spreadRatio15m, [-np.inf, 0.8, 1.2, 2.0, np.inf],
                       labels=["compressed", "normal", "expanded", "extreme"])
        groups["volatilityRegime"] = {str(value): stats(frame[bands == value]) for value in bands.dropna().unique()}
    return groups


def rejection_reasons(item: dict, config: ResearchConfig) -> list[str]:
    reasons = []
    minimums = {"train": config.min_train_closed, "validation": config.min_validation_closed,
                "outOfSample": config.min_oos_closed}
    for segment, minimum in minimums.items():
        if item[segment]["closed"] < minimum:
            reasons.append(f"{segment}_insufficient_sample")
        if item[segment]["netPnl"] <= 0:
            reasons.append(f"{segment}_net_not_positive")
    if item["outOfSample"]["maxDrawdown"] > config.max_oos_drawdown:
        reasons.append("outOfSample_drawdown_exceeded")
    if item["validation"]["profitFactor"] < 1.1:
        reasons.append("validation_profit_factor_below_1.1")
    if item["outOfSample"]["profitFactor"] < 1.1:
        reasons.append("outOfSample_profit_factor_below_1.1")
    return reasons


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
        item["rejectionReasons"] = rejection_reasons(item, config)
        item["qualified"] = not item["rejectionReasons"]
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
                       "selection": "train_rank_then_validation_then_reveal_oos",
                       "candidateComplexity": "fixed_threshold_atoms_and_max_two_clause_pairs"},
            "costs": {"notional": config.notional, "feeBps": config.fee_bps,
                      "taxBps": config.tax_bps, "slippageBps": config.slippage_bps},
            "baseline": {name: stats(trades[mask]) for name, mask in split.items()},
            "lossDiagnostics": diagnostics(trades),
            "qualificationPolicy": {"minimumClosed": {"train": config.min_train_closed,
                                                         "validation": config.min_validation_closed,
                                                         "outOfSample": config.min_oos_closed},
                                    "minimumProfitFactor": 1.1,
                                    "maximumOosDrawdown": config.max_oos_drawdown,
                                    "allSegmentsMustHavePositiveNetPnl": True},
            "qualifiedCandidates": qualified, "bestResearchCandidates": ranked[:10],
            "champion": {"gate": "NO_TRADE", "netPnl": 0.0, "maxDrawdown": 0.0},
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
