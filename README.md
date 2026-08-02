# Coin Platform Trading

Self-hosted cryptocurrency market-data and research platform for a Windows 11 Hyper-V home lab.

## Phase 1 scope

- Ubuntu Server 24.04 VM on Hyper-V
- Docker Compose deployment
- Binance public market data only
- BTCUSDT and ETHUSDT
- Aggregate trades and 1-minute candles
- Immutable newline-delimited RAW event files
- PostgreSQL for metadata foundation
- Prometheus-compatible collector metrics
- Provisioned Prometheus and Grafana monitoring dashboards
- Incremental Bronze-to-Silver Parquet pipeline with persistent checkpoints
- No exchange credentials and no live trading

## Architecture

```text
Binance WebSocket
       |
       v
Python Collector ----> /data/coin-platform/bronze/binance/...
       |
       +-------------> health and Prometheus metrics

PostgreSQL is reserved for metadata, pipeline state, strategy versions,
backtest runs, paper orders, and risk configuration.
```

The first implementation deliberately stores RAW events before introducing Parquet compaction. This keeps ingestion simple and auditable; a later processor will compact validated RAW files into partitioned Parquet.

## Local setup

1. Install Docker Engine and Docker Compose on Ubuntu Server 24.04.
2. Clone the repository and select the intended feature or release branch.
3. Copy `.env.example` to `.env` and change local passwords.
4. Create the data directories:

```bash
sudo mkdir -p /data/coin-platform/{bronze,quarantine,postgres}
sudo chown -R "$USER":"$USER" /data/coin-platform
```

5. Start the stack:

```bash
docker compose up -d --build
```

6. Check collector health:

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/metrics
```

7. Run the read-only Phase A audit:

```bash
./scripts/audit-phase-a.sh
```

## Operations

Common commands are intentionally run from the repository root:

```bash
docker compose up -d --build       # start or update
docker compose stop                # graceful stop; does not delete data
docker compose ps -a               # status
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/metrics
```

Do not use `docker compose down -v`: owner RAW data and PostgreSQL data must
never be removed as part of routine operations. See
[`docs/operations-runbook.md`](docs/operations-runbook.md) for health checks,
RAW and disk checks, unhealthy-service recovery, post-reboot validation, and
rollback procedures.

## Safety boundaries

- Phase 1 never places orders.
- No API key is required.
- RAW data is append-only.
- Secrets and collected datasets must never be committed.
- Any future execution service must pass through an independent risk engine.

See `AGENTS.md`, `docs/architecture.md`, and `docs/roadmap.md` before making major changes.

## Monitoring

Phase B monitoring is provisioned from `monitoring/` and stores persistent data
under `/data/coin-platform/{prometheus,grafana}`. See
[`docs/MONITORING_RUNBOOK.md`](docs/MONITORING_RUNBOOK.md) and run:

```bash
./scripts/audit-phase-b.sh
```

## Silver pipeline

Silver processing is documented in
[`docs/SILVER_PIPELINE_RUNBOOK.md`](docs/SILVER_PIPELINE_RUNBOOK.md). Validate it
with `./scripts/audit-phase-c.sh`; Bronze remains read-only and immutable.
