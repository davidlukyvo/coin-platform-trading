# Hybrid Signal Adapter — Shadow Phase

## Safety boundary

`hybrid-signal-adapter` is an observe-only compatibility layer inspired by the
authority contract in `SystemTrader_Hybrid`. It reads closed Silver candles and
writes a separate signal projection. It has no exchange credentials, order API,
paper broker connection, published host port, or capability to promote a signal
into execution.

Even when `authorityDecision=ALLOW`, both `executionActionable` and
`executionGatePassed` remain `false`. This is deliberate and covered by tests.

## Phase 1 model

- Source: closed Binance Silver 1-minute candles.
- Symbols: BTCUSDT and ETHUSDT.
- Trend: EMA(12) > EMA(26) > EMA(50).
- Trigger: close above the prior bar high.
- Context: ATR(14), 20-bar relative volume, synthetic 2R floor.
- Contract: `finalAuthorityStatus`, `authorityDecision`, `authorityReason`,
  `authorityTrace`, and a deterministic signal id.
- Output: `${COIN_DATA_ROOT}/hybrid-signals/latest-signals.json`.

This is not a byte-for-byte port of Alpha Guard. It establishes the service,
data, persistence, safety, and observability contracts needed to port and
validate individual Hybrid authority rules without coupling them to execution.

## Acceptance

```bash
docker compose config --quiet
docker compose build hybrid-signal-adapter
docker compose up --no-deps storage-init
docker compose up -d --no-deps hybrid-signal-adapter
docker compose exec -T hybrid-signal-adapter python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8060/healthz').read().decode())"
docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
```

The health response must contain `mode=SHADOW_ONLY` and `live_trading=false`.
Soak the adapter without changing EMA paper behavior, then compare its decisions
against the original SystemTrader Hybrid on the same closed-bar fixtures.
