from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class Source:
    exchange: str
    market: str
    feed: str
    symbol: str
    stream: str


@dataclass(frozen=True)
class Quality:
    earliest: float
    latest: float
    last_close: float
    rows_total: int
    expected_total: int
    missing_total: int
    rows_today: int
    expected_today: int
    missing_today: int
    trailing_today: int
    completeness_total: float
    completeness_today: float
    lag_seconds: float
    status: int


def parse_sources(value: str) -> tuple[Source, ...]:
    result = []
    for item in value.split(";"):
        exchange, market, feed, symbol, stream = (part.strip() for part in item.split(":"))
        if not all((exchange, market, feed, symbol, stream)):
            raise ValueError("empty source field")
        result.append(Source(exchange, market, feed, symbol.upper(), stream))
    if len(result) != len(set(result)):
        raise ValueError("duplicate source")
    return tuple(result)


def expected_minutes(first: float, last: float) -> int:
    if last < first:
        return 0
    return int(math.floor((last - first) / 60.0)) + 1


def trailing_minutes(latest: float, expected_latest: float) -> int:
    """Count only candles after the latest available candle, never time before a source began."""
    return max(0, expected_minutes(latest + 60.0, expected_latest))


def classify(lag: float, completeness: float, warning_lag: int, stale_lag: int) -> int:
    if lag > stale_lag or completeness < 0.98:
        return 0
    if lag > warning_lag or completeness < 0.995:
        return 1
    return 2


def scan(root: Path, source: Source, now: float, warning_lag: int = 1800, stale_lag: int = 3900) -> Quality:
    pattern = str(root / source.exchange / source.market / source.stream / f"symbol={source.symbol}" / "date=*" / "*.parquet")
    midnight = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    closed_minute = math.floor(now / 60.0) * 60.0 - 60.0
    row = duckdb.connect().execute(
        """WITH closed AS (
               SELECT DISTINCT open_time, close_time FROM read_parquet(?) WHERE is_closed
             )
             SELECT epoch(min(open_time)), epoch(max(open_time)), epoch(max(close_time)), count(*),
                    count(*) FILTER (WHERE epoch(open_time) >= ? AND epoch(open_time) <= ?)
             FROM closed""",
        [pattern, midnight, closed_minute],
    ).fetchone()
    if not row or row[0] is None:
        raise ValueError("no_closed_candles")
    earliest, latest, last_close, rows_total, rows_today = float(row[0]), float(row[1]), float(row[2]), int(row[3]), int(row[4])
    expected_total = expected_minutes(earliest, latest)
    today_start = max(midnight, earliest)
    expected_today = expected_minutes(today_start, min(latest, closed_minute)) if latest >= today_start else 0
    missing_total = max(0, expected_total - rows_total)
    missing_today = max(0, expected_today - rows_today)
    trailing_today = trailing_minutes(latest, closed_minute)
    completeness_total = rows_total / expected_total if expected_total else 0.0
    completeness_today = rows_today / expected_today if expected_today else 0.0
    lag = max(0.0, now - last_close)
    status = classify(lag, completeness_today, warning_lag, stale_lag)
    return Quality(earliest, latest, last_close, rows_total, expected_total, missing_total, rows_today,
                   expected_today, missing_today, trailing_today, completeness_total, completeness_today, lag, status)


def timeframe_readiness(quality: Quality) -> dict[str, int]:
    minimum_minutes = {"1m": 20, "5m": 100, "15m": 300, "1h": 1200, "4h": 4800}
    healthy = quality.status > 0 and quality.completeness_today >= 0.98
    return {timeframe: int(healthy and quality.rows_total >= required)
            for timeframe, required in minimum_minutes.items()}
