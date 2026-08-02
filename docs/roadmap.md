# Roadmap

## Milestone 1 — Data foundation

- Provision Ubuntu VM and separate data disk
- Run Docker Compose stack
- Collect BTCUSDT and ETHUSDT aggregate trades and 1-minute candles
- Validate reconnect, restart, disk growth, and health metrics
- Add automated unit and integration tests

## Milestone 2 — Validation and Parquet

- RAW file rotation and checksums
- Schema validation and quarantine
- Duplicate and missing-candle checks
- Silver normalization
- Partitioned Parquet with Zstandard compression
- DuckDB research queries

## Milestone 3 — Observability and research

- Prometheus and Grafana
- Data freshness, event rate, errors, disk utilization, and gap dashboards
- JupyterLab research environment
- Technical and market-microstructure feature pipeline

## Milestone 4 — Backtesting

- Versioned strategies and datasets
- Fee, spread, latency, and slippage modeling
- Walk-forward and out-of-sample validation
- Reproducible backtest reports

## Milestone 5 — Paper trading

- Signal engine
- Independent risk engine
- Paper execution and trade journal
- Daily-loss limit and kill switch testing

## Milestone 6 — Controlled live pilot

Live execution remains prohibited until prior milestones pass documented acceptance criteria. Any pilot must use a dedicated sub-account, minimum capital, withdrawal-disabled keys, IP restrictions, and hard risk limits.
