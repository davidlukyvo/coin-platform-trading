#!/usr/bin/env bash
set -euo pipefail
cd "${1:-$HOME/coin-platform-trading}"

docker compose exec -T silver-processor python - <<'PY'
import duckdb
checks = {
 "aggTrade": "/data/silver/binance/spot/aggTrade/symbol=*/date=*/*.parquet",
 "kline_1m": "/data/silver/binance/spot/kline_1m/symbol=*/date=*/*.parquet",
}
con = duckdb.connect()
for stream, pattern in checks.items():
    rows = con.execute("select count(*) from glob(?)", [pattern]).fetchone()[0]
    if rows == 0:
        raise SystemExit(f"no parquet files for {stream}")
    if stream == "aggTrade":
        query = "select count(*), count(distinct symbol || ':' || aggregate_trade_id), min(exchange_event_time), max(exchange_event_time) from read_parquet(?)"
    else:
        query = "select count(*), count(distinct symbol || ':' || interval || ':' || cast(open_time as varchar)), min(exchange_event_time), max(exchange_event_time), count(*) filter(where not is_closed) from read_parquet(?)"
    result = con.execute(query, [pattern]).fetchone()
    if result[0] != result[1]:
        raise SystemExit(f"duplicate keys in {stream}: {result[:2]}")
    if stream == "kline_1m" and result[4] != 0:
        raise SystemExit("open kline found in Silver")
    print(stream, result)
PY
