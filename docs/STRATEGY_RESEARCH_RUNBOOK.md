# Strategy Research Runbook

Phase D reads closed Silver one-minute candles only. It never places orders and never writes to Bronze or Silver.

## Assumptions

- Signals use information available at candle close and execute at the next candle open.
- Results include configurable fees, slippage and volatility-based position sizing.
- Train/validation/out-of-sample splits are chronological (60/20/20), never shuffled.
- Results with less than 30 days of one-minute history are labelled `EXPLORATORY`.

## Operations

Build and start only the research service with `docker compose up -d --build strategy-research`.
Check `docker compose ps strategy-research`, its `/healthz` endpoint inside the container, and
`/data/coin-platform/research/latest.json`. Grafana dashboard: **Strategy Research & Backtesting**.

To roll back, stop only `strategy-research` and revert the Phase D commit. Existing market data is unaffected.
