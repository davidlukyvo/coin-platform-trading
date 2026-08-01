# Agent Instructions

## Mission

Build a lightweight, production-oriented cryptocurrency data and research platform that runs on an Ubuntu Server 24.04 VM hosted by Windows 11 Hyper-V.

## Current hardware

- Intel NUC, Core i7 6th generation
- 32 GB RAM
- 1 TB SSD
- Windows 11 Hyper-V host

## Non-negotiable architecture rules

1. RAW market data is append-only and immutable.
2. Use UTC internally; Asia/Ho_Chi_Minh is display-only.
3. Never commit credentials, tokens, certificates, passwords, datasets, or database files.
4. Phase 1 uses Binance public market data and cannot place orders.
5. Future signal generation must never bypass an independent risk engine.
6. Future exchange keys must have withdrawal disabled and use IP restrictions where supported.
7. Every long-running service must expose health and metrics endpoints.
8. Data-quality failures must be visible and must not be silently repaired.
9. Major architecture choices belong in `docs/decisions/`.
10. Prefer simple, reversible components over premature clustering.

## Initial stack

- Python 3.12
- Docker Compose
- PostgreSQL 16
- RAW NDJSON followed by validated Parquet compaction
- DuckDB for research
- Prometheus metrics
- Grafana in a later milestone

## Engineering workflow

- Work on feature branches.
- Do not commit directly to `main` after repository bootstrap.
- Keep changes small and reviewable.
- Add tests for parsing, normalization, partition paths, reconnect behavior, and risk rules.
- Update documentation with behavior or architecture changes.
- Prefer type hints, structured logging, explicit timeouts, and bounded retries.
- Preserve unknown source fields in the RAW payload.

## Resource limits

The first VM is expected to have roughly 6 vCPU, 16 GB RAM, an 80 GB OS disk, and a separate 500-650 GB data disk. Avoid Kubernetes, Spark, Kafka, and GPU dependencies until measurements justify them.
