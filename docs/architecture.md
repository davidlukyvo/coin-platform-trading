# Architecture

## Phase 1 deployment

```text
Windows 11 Hyper-V Host
└── Ubuntu Server 24.04 VM
    └── Docker Compose
        ├── collector
        │   ├── Binance combined WebSocket
        │   ├── append-only RAW writer
        │   ├── /healthz
        │   └── /metrics
        └── PostgreSQL 16
```

## Data path

```text
/data/coin-platform/
├── bronze/
│   └── binance/spot/<stream>/symbol=<SYMBOL>/date=YYYY-MM-DD/hour=HH/events.ndjson
├── quarantine/
└── postgres/
```

RAW records contain exchange identity, market type, stream name, exchange event timestamp, collector receive timestamp, schema version, and the untouched source payload.

## Design decisions

- Phase 1 uses public streams only, so no exchange credentials are present.
- NDJSON is the ingestion format because it is simple, append-only, and easy to recover. A separate validated processor will later compact files into Parquet.
- PostgreSQL is not used for high-volume ticks. It is reserved for metadata and future control-plane entities.
- All timestamps are UTC internally.
- Kafka, Spark, Kubernetes, ClickHouse, and machine learning are deferred until measured workload requires them.

## Failure behavior

- WebSocket failures trigger bounded exponential reconnect backoff.
- Health becomes unhealthy when disconnected or when no event arrives for 60 seconds.
- Write or decode failures increment error metrics and never fabricate data.
- Future sequence-sensitive order-book ingestion must quarantine gaps and rebuild from an authoritative snapshot.
