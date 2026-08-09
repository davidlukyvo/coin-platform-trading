# Paper Trading and Risk Engine Runbook

## Safety contract

`paper-trading` is synthetic only. It mounts no credential directory, has no host port, and joins only the internal `paper-control` Docker network. Its health response always includes `mode=PAPER_ONLY` and `live_trading=false`.

## Default account and limits

- Starting paper cash: 10,000 USDT
- Data: closed Binance Silver 1-minute candles
- Strategy: EMA(12/26) trend, Spot long/flat
- Symbols: BTCUSDT and ETHUSDT
- Target/order: 100 USDT
- Maximum symbol exposure: 150 USDT
- Maximum gross exposure: 250 USDT
- Daily-loss limit: 25 USDT
- Drawdown limit: 2%
- Trades per UTC day: 20
- Fee/slippage: 4/2 bps
- Market age: 3900 seconds because current small Silver kline batches seal hourly

## Journal

Synthetic state lives under `${COIN_DATA_ROOT}/paper`:

- `paper.db`: SQLite journal with signals, risk events, orders, fills, positions, and equity;
- `latest-state.json`: atomic read-only projection used by System Trading UI.

Do not reset or edit the journal during a soak. Paper data is not real exchange data, but it is still evidence needed to evaluate the strategy and risk controls.

## Verification

```bash
docker compose ps paper-trading
docker inspect trading-bamboo-paper-trading-1 --format '{{json .NetworkSettings.Networks}}'
docker exec trading-bamboo-paper-trading-1 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8050/healthz').read().decode())"
docker exec trading-bamboo-prometheus-1 wget -qO- http://paper-trading:8050/metrics | grep '^paper_trading_'
```

The container must have only the internal `paper-control` network and no published port. The System Trading UI and Grafana dashboard **Paper Trading & Risk Engine** provide read-only views.

## Kill switch

Set `PAPER_KILL_SWITCH=true` and recreate only `paper-trading` to reject new paper orders. Existing paper positions remain synthetic and unchanged. This switch does not control any exchange account because no live execution code exists.

## Rollback

Stop only the paper service:

```bash
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml stop paper-trading
```

Stopping paper trading does not affect collectors, Silver, research, System Trading credentials, or market data.
