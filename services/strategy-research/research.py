from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


@dataclass
class Result:
    symbol: str; strategy: str; segment: str; bars: int; trades: int
    net_return: float; max_drawdown: float; sharpe: float; win_rate: float
    fee_bps: float; slippage_bps: float; confidence: str


def load_bars(root: Path, symbol: str) -> pd.DataFrame:
    pattern = str(root / "binance/spot/kline_1m" / f"symbol={symbol}" / "date=*" / "*.parquet")
    query = """select open_time, open, high, low, close, volume from read_parquet(?)
               where is_closed order by open_time"""
    df = duckdb.connect().execute(query, [pattern]).df()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    return df.drop_duplicates("open_time", keep="last").reset_index(drop=True)


def features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = out.close.pct_change()
    out["ema_fast"] = out.close.ewm(span=12, adjust=False).mean()
    out["ema_slow"] = out.close.ewm(span=26, adjust=False).mean()
    delta = out.close.diff(); gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    rs = gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
    out["rsi"] = 100 - 100 / (1 + rs)
    out["vol"] = out.ret.rolling(60).std().shift(1)
    return out


def signal(df: pd.DataFrame, strategy: str) -> pd.Series:
    # Signal is decided at close[t]; execution is always open[t+1].
    if strategy == "ema_trend":
        raw = (df.ema_fast > df.ema_slow).astype(float)
    elif strategy == "rsi_reversion":
        state = np.where(df.rsi < 30, 1.0, np.where(df.rsi > 55, 0.0, np.nan))
        raw = pd.Series(state, index=df.index).ffill().fillna(0)
    else:
        raise ValueError(strategy)
    return raw.shift(1).fillna(0)


def backtest(df: pd.DataFrame, symbol: str, strategy: str, segment: str,
             fee_bps: float = 4.0, slippage_bps: float = 2.0,
             capital_fraction: float = 0.25, target_vol: float = 0.001) -> Result:
    f = features(df)
    desired = signal(f, strategy)
    size = (target_vol / f.vol.replace(0, np.nan)).clip(upper=1).fillna(0) * capital_fraction
    position = desired * size
    open_return = f.open.shift(-1) / f.open - 1
    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * (fee_bps + slippage_bps) / 10000
    pnl = (position * open_return - costs).fillna(0)
    equity = (1 + pnl).cumprod()
    drawdown = equity / equity.cummax() - 1
    exits = (position.diff() < 0)
    trade_pnl = pnl.groupby((turnover > 0).cumsum()).sum()
    confidence = "RESEARCH" if len(df) >= 30 * 24 * 60 else "EXPLORATORY"
    sharpe = 0.0 if pnl.std() == 0 else float(pnl.mean() / pnl.std() * np.sqrt(365 * 24 * 60))
    return Result(symbol, strategy, segment, len(df), int(exits.sum()), float(equity.iloc[-1] - 1),
                  float(drawdown.min()), sharpe, float((trade_pnl > 0).mean()), fee_bps, slippage_bps, confidence)


def walk_forward(df: pd.DataFrame, symbol: str, strategy: str) -> list[Result]:
    n = len(df); train_end = int(n * .6); validation_end = int(n * .8)
    return [backtest(df.iloc[:train_end], symbol, strategy, "train"),
            backtest(df.iloc[train_end:validation_end], symbol, strategy, "validation"),
            backtest(df.iloc[validation_end:], symbol, strategy, "out_of_sample")]


def run(root: Path, output: Path) -> list[Result]:
    results = [r for symbol in ("BTCUSDT", "ETHUSDT") for strategy in ("ema_trend", "rsi_reversion")
               for r in walk_forward(load_bars(root, symbol), symbol, strategy)]
    output.mkdir(parents=True, exist_ok=True)
    temp = output / "latest.json.tmp"
    temp.write_text(json.dumps([asdict(x) for x in results], indent=2) + "\n", encoding="utf-8")
    temp.replace(output / "latest.json")
    return results
