# Silver Pipeline Runbook

The Silver processor converts immutable Bronze NDJSON to fixed-schema Zstd
Parquet. It runs every 60 seconds with a 16 MiB batch cap and uses a persistent
SQLite checkpoint under `/data/coin-platform/checkpoints/silver`.

## Data contract

Aggregate trades use `symbol + aggregate_trade_id` as the unique key. Prices
and quantities are Decimal128(38,18). Klines use `symbol + interval + open_time`;
only the latest update in a batch is retained and only `is_closed=true` candles
are published. All timestamps are timezone-aware UTC.

The output is partitioned by stream, symbol, and UTC date. File names are a
deterministic hash of the Bronze path and byte-offset range, making crash retry
safe. Parquet is written to a temporary file, fsynced, and atomically renamed
before the checkpoint transaction commits.

## Operations

```bash
docker compose up -d --build silver-processor
docker compose ps silver-processor
./scripts/audit-phase-c.sh
./scripts/validate-silver.sh
```

Do not reset the checkpoint or delete Silver without owner approval. Restart
only Silver when troubleshooting:

```bash
docker compose logs --since=30m --no-color silver-processor
docker compose restart silver-processor
./scripts/audit-phase-c.sh
```

## Backfill modes

Dry-run is the default and does not write or alter the incremental checkpoint:

```bash
./scripts/backfill-silver.sh "$PWD" 2026-08-01 2026-08-01 --dry-run
```

`--write-without-checkpoint` writes deterministic Parquet but preserves the
incremental checkpoint. `--write-and-commit-checkpoint` also advances it and
requires explicit operational approval. A full-history backfill requires owner
approval regardless of mode.

## Data quality and quarantine

Each committed batch writes a JSON summary under `reports/silver`. Malformed or
invalid records are written separately under `silver-quarantine`; Bronze is
never changed. A partial final NDJSON line is left unread and its offset is not
committed until the newline arrives.

## Rollback

Stop only Silver, revert the Phase C commit, and rebuild the prior Compose
configuration. Preserve Silver, reports, quarantine, and checkpoints for
inspection. Never remove Bronze or reset checkpoint state as rollback.
