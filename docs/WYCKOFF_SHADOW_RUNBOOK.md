# Wyckoff Shadow Adapter v1

This service converts closed Silver one-minute candles into compact, auditable
Wyckoff research signals. It is deliberately observe-only: it has no exchange
credentials, order API, published host port, or route to Paper/Live execution.

## Data contract and storage

- Canonical source: closed Binance Silver `kline_1m` Parquet.
- Derived in memory: exact complete 5m, 15m and 1h candles.
- 15m detects range context and Spring/Upthrust/SOS/SOW/Test candidates.
- 5m confirms the trigger; 1h EMA20/EMA50 supplies directional context.
- Output: `${COIN_DATA_ROOT}/wyckoff-signals/latest-signals.json`.
- Audit/backtest journal: `signals.db`, one row per symbol and closed 5m bar.
- A deterministic signal ID and SQLite primary key make repeated cycles idempotent.

The compact journal stores features, candidates, decisions, entry/stop/target and
real reward/risk. It never stores raw exchange credentials. Silver remains the
long-term replay source. Bronze retention is intentionally unchanged; deletion
must be designed and approved separately after restore/replay testing.

## Interpretation

`ALLOW` means a research setup passed the shadow gate. It is not an order.
Every result is fixed to `SHADOW_ONLY`, `liveTrading=false`,
`executionActionable=false`, and `executionGatePassed=false`.

## Safe deployment

```sh
docker compose config --quiet
python3 -m venv /tmp/wyckoff-test
/tmp/wyckoff-test/bin/pip install -r services/wyckoff-shadow-adapter/requirements.txt -r services/wyckoff-shadow-adapter/requirements-dev.txt
/tmp/wyckoff-test/bin/pytest -q services/wyckoff-shadow-adapter
docker compose build wyckoff-shadow-adapter
docker compose up --no-deps storage-init
docker compose up -d --no-deps wyckoff-shadow-adapter
```

Validate `/healthz`, projection freshness, journal growth, Prometheus target and
alerts for 48–72 hours. Do not reset the journal to restart a soak.
