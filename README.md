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
- No exchange credentials and no live trading

## Architecture

```text
Binance WebSocket
       |
       v
Python Collector ----> /data/bronze/binance/...
       |
       +-------------> health and Prometheus metrics

PostgreSQL is reserved for metadata, pipeline state, strategy versions,
backtest runs, paper orders, and risk configuration.
```

The first implementation deliberately stores RAW events before introducing Parquet compaction. This keeps ingestion simple and auditable; a later processor will compact validated RAW files into partitioned Parquet.

## Local setup

1. Install Docker Engine and Docker Compose on Ubuntu Server 24.04.
2. Clone this private repository.
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

## Safety boundaries

- Phase 1 never places orders.
- No API key is required.
- RAW data is append-only.
- Secrets and collected datasets must never be committed.
- Any future execution service must pass through an independent risk engine.

See `AGENTS.md`, `docs/architecture.md`, and `docs/roadmap.md` before making major changes.
