# ADR 0004: Paper trading and independent risk boundary

- Status: Accepted
- Date: 2026-08-09

## Context

The research service produces exploratory backtests but cannot validate continuous signal handling, execution costs, exactly-once behavior, journals, or operational risk controls. Real orders remain prohibited.

## Decision

Create an isolated `paper-trading` service with these boundaries:

- read only closed Binance Silver candles and never read Bronze directly;
- run the causal EMA trend signal for BTCUSDT and ETHUSDT;
- simulate Spot long/flat fills with explicit fee and slippage;
- persist synthetic account, signals, risk decisions, orders, fills, positions, and equity in a dedicated SQLite journal;
- require every order intent to pass an independent `RiskEngine` before paper fill;
- enforce symbol allowlist, stale-data rejection, duplicate signal protection, order/symbol/gross limits, daily-loss limit, drawdown limit, trade-count limit, and kill switch;
- mount no exchange credentials and attach only to an internal Docker network shared with Prometheus, with no published port or Internet route;
- export a read-only JSON state for the authenticated System Trading UI;
- label all state and health responses `PAPER_ONLY` and `live_trading=false`.

The current Silver processor seals small kline batches at the hour boundary. The paper stale-data threshold is therefore 3900 seconds; signals are evaluated once per new sealed candle and duplicate IDs prevent replay. Reducing this threshold requires first improving Silver kline publication latency.

## Consequences

Paper fills are deterministic approximations, not exchange guarantees. They exclude order-book depth, partial fills, latency tails, and market impact. Passing a paper soak does not authorize live trading. A future execution adapter must be a separate service behind the same risk contract and requires explicit owner approval.
