"""Read-only, no-lookahead walk-forward evaluator for Wyckoff ALLOW evidence."""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import pandas as pd


@dataclass
class Segment:
    name: str
    signals: int
    closed: int
    wins: int
    losses: int
    unresolved: int
    gross_pnl: float
    fees: float
    taxes: float
    net_pnl: float
    max_drawdown: float


def load_signals(path: Path, symbol: str) -> pd.DataFrame:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    query = """SELECT signal_id,market_time,direction,entry,stop,target,rr
               FROM signals WHERE symbol=? AND decision='ALLOW'
               AND direction IN ('LONG','SHORT') ORDER BY market_time"""
    frame = pd.read_sql_query(query, connection, params=(symbol,))
    connection.close()
    frame["market_time"] = pd.to_datetime(frame["market_time"], utc=True)
    return frame


def load_bars(root: Path, symbol: str) -> pd.DataFrame:
    pattern = str(root / "binance/spot/kline_1m" / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """SELECT open_time,close_time,open,high,low,close FROM read_parquet(?)
               WHERE is_closed QUALIFY row_number() OVER
               (PARTITION BY open_time ORDER BY exchange_event_time DESC)=1 ORDER BY open_time"""
    frame = duckdb.connect().execute(query, [pattern]).df()
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], utc=True)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column])
    return frame


def evaluate(signals: pd.DataFrame, bars: pd.DataFrame, name: str, notional: float,
             fee_bps: float, tax_bps: float, slippage_bps: float,
             max_holding_bars: int = 24 * 60) -> Segment:
    gross = fees = taxes = net = 0.0
    wins = losses = unresolved = 0
    curve = [0.0]
    for signal in signals.itertuples(index=False):
        future = bars[bars.open_time > signal.market_time].head(max_holding_bars)
        if future.empty:
            unresolved += 1
            continue
        side = signal.direction
        entry_slip = 1 + (slippage_bps / 10000 if side == "LONG" else -slippage_bps / 10000)
        entry = float(signal.entry) * entry_slip
        quantity = notional / entry
        exit_price = None
        for bar in future.itertuples(index=False):
            stop_hit = bar.low <= signal.stop if side == "LONG" else bar.high >= signal.stop
            target_hit = bar.high >= signal.target if side == "LONG" else bar.low <= signal.target
            if stop_hit:
                exit_price = float(signal.stop)
                break
            if target_hit:
                exit_price = float(signal.target)
                break
        if exit_price is None:
            unresolved += 1
            continue
        exit_price *= 1 + (-slippage_bps / 10000 if side == "LONG" else slippage_bps / 10000)
        exit_notional = quantity * exit_price
        trade_gross = quantity * (exit_price - entry) * (1 if side == "LONG" else -1)
        trade_fees = (notional + exit_notional) * fee_bps / 10000
        trade_tax = (exit_notional if side == "LONG" else notional) * tax_bps / 10000
        trade_net = trade_gross - trade_fees - trade_tax
        gross += trade_gross
        fees += trade_fees
        taxes += trade_tax
        net += trade_net
        wins += int(trade_net > 0)
        losses += int(trade_net <= 0)
        curve.append(net)
    peak = curve[0]
    max_drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    return Segment(name, len(signals), wins + losses, wins, losses, unresolved,
                   gross, fees, taxes, net, max_drawdown)


def walk_forward(signals: pd.DataFrame, bars: pd.DataFrame, **kwargs) -> list[Segment]:
    first = int(len(signals) * 0.6)
    second = int(len(signals) * 0.8)
    parts = (("train", signals.iloc[:first]), ("validation", signals.iloc[first:second]),
             ("out_of_sample", signals.iloc[second:]))
    return [evaluate(part, bars, name, **kwargs) for name, part in parts]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals-db", type=Path, required=True)
    parser.add_argument("--silver-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--notional", type=float, default=200)
    parser.add_argument("--fee-bps", type=float, default=5)
    parser.add_argument("--tax-bps", type=float, default=10)
    parser.add_argument("--slippage-bps", type=float, default=2)
    args = parser.parse_args()
    signals = load_signals(args.signals_db, args.symbol)
    bars = load_bars(args.silver_root, args.symbol)
    results = walk_forward(signals, bars, notional=args.notional, fee_bps=args.fee_bps,
                           tax_bps=args.tax_bps, slippage_bps=args.slippage_bps)
    payload = {"mode": "OFFLINE_RESEARCH_ONLY", "symbol": args.symbol,
               "assumptions": {"notional": args.notional, "feeBps": args.fee_bps,
                               "taxBps": args.tax_bps, "slippageBps": args.slippage_bps,
                               "sameBarPolicy": "STOP_FIRST_CONSERVATIVE", "funding": "NOT_INSTRUMENTED"},
               "sourceSignals": len(signals), "segments": [asdict(x) for x in results]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
