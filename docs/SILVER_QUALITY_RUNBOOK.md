# Silver Quality Exporter

`silver-quality-exporter` is a read-only, low-cardinality view of closed Silver
one-minute Parquet candles. It does not write Silver, mount credentials, publish
a host port, or participate in execution.

## Sources of truth

- Source/market/feed/base timeframe: explicit Compose configuration matching the
  deployed collectors and Silver directory contract.
- Freshness, rows, gaps, completeness and historical bounds: closed Parquet
  candle timestamps read with DuckDB.
- Pipeline throughput/errors: existing Silver Processor Prometheus metrics.
- Storage size/growth: existing node textfile storage metrics.

The source list is fixed in configuration. Labels are bounded to four deployed
exchange/symbol combinations; no event IDs, timestamps or filenames become
Prometheus labels.

## Semantics

- Expected candles assume a continuous 24/7 one-minute crypto market between
  the first and last observed candle.
- `today` is UTC and ends at the last fully closed minute.
- Status: `2` healthy, `1` delayed/minor gap, `0` stale/materially incomplete.
- Per-symbol lag defaults are yellow after 30 minutes and red after 65 minutes,
  matching the current hourly/batched Silver sealing model. The separate Silver
  processor heartbeat remains yellow/red at 120/180 seconds.
- 5m/15m/1h/4h readiness means derivable on demand from enough healthy 1m
  history. It does not mean live execution is enabled.
- Backfill state is not exposed because no authoritative persistent backfill job
  state exists yet. The dashboard labels this as `NOT INSTRUMENTED`.

## Validation

```sh
docker build --target test services/silver-quality-exporter
docker compose config --quiet
docker compose build silver-quality-exporter
docker compose up -d --no-deps silver-quality-exporter
```
