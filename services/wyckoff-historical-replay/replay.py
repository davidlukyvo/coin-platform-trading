from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from loss_engine import ResearchConfig, candidate_masks, outcomes, stats
from wyckoff_engine import Thresholds, classify_event, evaluate, resample_closed


ROOT = Path(os.getenv("REPLAY_DATA_ROOT", "/data/replay"))
SYMBOL = os.getenv("REPLAY_SYMBOL", "BTCUSDT")


def load_minutes() -> pd.DataFrame:
    files = sorted((ROOT / "parquet").glob("month=*/bars.parquet"))
    if not files:
        raise FileNotFoundError("historical_parquet_not_found")
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last").reset_index(drop=True)
    return frame


def candidate_times(minutes: pd.DataFrame) -> list[pd.Timestamp]:
    bars_15m = resample_closed(minutes, "15min")
    selected = []
    for index in range(40, len(bars_15m)):
        window = bars_15m.iloc[max(0, index - 30):index + 1]
        if classify_event(window, Thresholds()) != "NONE":
            close_time = pd.Timestamp(bars_15m.close_time.iloc[index])
            selected.extend([close_time + pd.Timedelta(minutes=offset) for offset in (0, 5, 10)])
    return sorted(set(selected))


def generate_signals(minutes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    opens = pd.to_datetime(minutes.open_time, utc=True)
    for current in candidate_times(minutes):
        end = int(opens.searchsorted(current, side="right"))
        if end < 1800:
            continue
        window = minutes.iloc[max(0, end - 6500):end]
        signal = evaluate(window, "binance", SYMBOL, Thresholds(), now=current.timestamp())
        if signal["authorityDecision"] != "ALLOW":
            continue
        row = {"signal_id": signal["signalId"], "market_time": pd.Timestamp(signal["marketTime"]),
               "phase": signal["phaseCandidate"], "event": signal["eventCandidate"],
               "direction": signal["direction"], "score": signal["score"], "entry": signal["entry"],
               "stop": signal["stop"], "target": signal["target"], "rr": signal["rr"], **signal["features"]}
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates("signal_id").sort_values("market_time").reset_index(drop=True)


def rolling_walk_forward(trades: pd.DataFrame, config: ResearchConfig) -> dict:
    start, end = trades.market_time.min(), trades.market_time.max()
    # The final two boundaries define a genuinely unseen holdout. Gate ranking
    # only sees rows before holdout_start; holdout results are revealed once.
    boundaries = pd.date_range(start=start.floor("D"), end=end.ceil("D"), periods=10)
    atoms = candidate_masks(trades)
    folds = []
    for index in range(4, 8):
        train_end, validation_end = boundaries[index], boundaries[index + 1]
        train_mask = trades.market_time < train_end
        validation_mask = (trades.market_time >= train_end) & (trades.market_time < validation_end)
        eligible = [name for name in atoms
                    if stats(trades[train_mask & atoms[name]])["closed"] >= config.min_train_closed]
        ranked = sorted(eligible or ["all"],
                        key=lambda name: stats(trades[train_mask & atoms[name]])["netPnl"], reverse=True)
        champion = ranked[0]
        folds.append({"trainEnd": train_end.isoformat(), "validationEnd": validation_end.isoformat(),
                      "selectedGate": champion, "train": stats(trades[train_mask & atoms[champion]]),
                      "validation": stats(trades[validation_mask & atoms[champion]])})
    holdout_start = boundaries[8]
    development = trades.market_time < holdout_start
    frozen = trades.market_time >= holdout_start
    eligible = [name for name in atoms
                if stats(trades[development & atoms[name]])["closed"] >= config.min_train_closed]
    ranked = sorted(eligible or ["all"],
                    key=lambda name: (stats(trades[development & atoms[name]])["netPnl"],
                                      stats(trades[development & atoms[name]])["closed"]), reverse=True)
    shortlist = ranked[:10]
    holdout = []
    for name in shortlist:
        holdout.append({"gate": name, "development": stats(trades[development & atoms[name]]),
                        "frozenHoldout": stats(trades[frozen & atoms[name]])})
    return {"folds": folds, "shortlist": holdout,
            "holdoutStart": holdout_start.isoformat(),
            "holdoutPolicy": "selected_on_development_then_revealed_once",
            "promotionAllowed": False}


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    minutes = load_minutes()
    signals = generate_signals(minutes)
    if signals.empty:
        raise ValueError("no_historical_allow_signals")
    config = ResearchConfig(symbol=SYMBOL)
    trades = outcomes(signals, minutes[["open_time", "high", "low", "close"]], config)
    result = rolling_walk_forward(trades, config)
    identity = hashlib.sha256((manifest["months"][0]["sha256"] + manifest["months"][-1]["sha256"] +
                               str(len(signals))).encode()).hexdigest()
    report = {"version": "wyckoff_historical_replay_v1", "reportId": identity,
              "updatedAt": datetime.now(UTC).isoformat(), "mode": "OFFLINE_RESEARCH_ONLY",
              "liveTrading": False, "executionActionable": False, "executionGatePassed": False,
              "symbol": SYMBOL, "months": manifest["requestedMonths"], "minuteRows": len(minutes),
              "allowSignals": len(signals), "resolvedTrades": int(trades.resolved.sum()),
              "costs": {"feeBps": config.fee_bps, "taxBps": config.tax_bps,
                        "slippageBps": config.slippage_bps, "funding": "NOT_INSTRUMENTED"}, **result}
    output = ROOT / "reports"; output.mkdir(parents=True, exist_ok=True)
    signals.to_parquet(output / "allow-signals.parquet", index=False)
    trades.to_parquet(output / "trade-outcomes.parquet", index=False)
    temporary = output / "latest.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "latest.json")
    print(json.dumps(report, separators=(",", ":")))


if __name__ == "__main__":
    main()
