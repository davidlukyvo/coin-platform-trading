from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


SYMBOL = os.getenv("REPLAY_SYMBOL", "BTCUSDT")
START = os.getenv("REPLAY_START_MONTH", "2025-09")
END = os.getenv("REPLAY_END_MONTH", "2026-08")
ROOT = Path(os.getenv("REPLAY_DATA_ROOT", "/data/replay"))
BASE = "https://data.binance.vision/data/spot/monthly/klines"
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
           "quote_volume", "trade_count", "taker_buy_base", "taker_buy_quote", "ignore"]


def months(start: str, end: str) -> list[str]:
    values, current, stop = [], pd.Period(start, "M"), pd.Period(end, "M")
    while current <= stop:
        values.append(str(current)); current += 1
    return values


def fetch(url: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size:
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "TradingBambooResearch/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    temporary.replace(destination)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def convert_month(month: str) -> dict:
    archive_dir = ROOT / "archives"
    parquet_dir = ROOT / "parquet" / f"month={month}"
    archive_dir.mkdir(parents=True, exist_ok=True); parquet_dir.mkdir(parents=True, exist_ok=True)
    name = f"{SYMBOL}-1m-{month}.zip"
    archive, checksum = archive_dir / name, archive_dir / f"{name}.CHECKSUM"
    url = f"{BASE}/{SYMBOL}/1m/{name}"
    fetch(url, archive); fetch(f"{url}.CHECKSUM", checksum)
    expected = checksum.read_text(encoding="utf-8").strip().split()[0].lower()
    actual = sha256(archive)
    if expected != actual:
        raise ValueError(f"checksum_mismatch:{month}")
    output = parquet_dir / "bars.parquet"
    if not output.exists():
        with zipfile.ZipFile(archive) as package:
            members = [item for item in package.namelist() if item.endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"unexpected_archive_members:{month}")
            with package.open(members[0]) as source:
                frame = pd.read_csv(source, header=None, names=COLUMNS)
        for column in ("open", "high", "low", "close", "volume", "quote_volume",
                       "trade_count", "taker_buy_base", "taker_buy_quote"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        unit = "us" if int(frame.open_time.iloc[0]) > 100_000_000_000_000 else "ms"
        frame["open_time"] = pd.to_datetime(frame.open_time, unit=unit, utc=True)
        frame["close_time"] = pd.to_datetime(frame.close_time, unit=unit, utc=True)
        frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last")
        frame["exchange_event_time"] = frame.close_time
        frame["is_closed"] = True
        frame.to_parquet(output, index=False, compression="zstd")
    frame = pd.read_parquet(output, columns=["open_time", "close_time"])
    deltas = frame.open_time.sort_values().diff().dropna().dt.total_seconds()
    return {"month": month, "url": url, "sha256": actual, "rows": len(frame),
            "firstOpen": frame.open_time.min().isoformat(), "lastClose": frame.close_time.max().isoformat(),
            "internalMinuteGaps": int((deltas != 60).sum())}


def main() -> None:
    records = [convert_month(month) for month in months(START, END)]
    manifest = {"version": "binance_spot_1m_archive_v1", "symbol": SYMBOL,
                "requestedMonths": [START, END], "generatedAt": datetime.now(UTC).isoformat(),
                "source": "data.binance.vision", "checksumVerified": True,
                "months": records, "rows": sum(item["rows"] for item in records),
                "internalMinuteGaps": sum(item["internalMinuteGaps"] for item in records)}
    temporary = ROOT / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ROOT / "manifest.json")
    print(json.dumps(manifest, separators=(",", ":")))


if __name__ == "__main__":
    main()
