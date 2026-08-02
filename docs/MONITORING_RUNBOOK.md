# Monitoring Runbook

Phase B adds Prometheus, Grafana, Node Exporter, cAdvisor, and a small storage
metrics writer. Prometheus, exporters, and cAdvisor are internal-only. Grafana
defaults to `127.0.0.1:3000`; set `MONITORING_BIND_ADDRESS=192.168.1.10` only for
access from the trusted home LAN. No alert receiver is configured.

## Sizing and retention

Prometheus is limited to 384 MiB RAM, 0.75 CPU, 15 days, and 2 GB TSDB data.
Grafana is limited to 384 MiB; cAdvisor to 192 MiB; Node Exporter to 96 MiB;
the storage scanner to 64 MiB. This protects the 167 GB data disk and a VM that
may balloon down to roughly 2 GB when idle.

## Start and validate

```bash
docker compose config --quiet
docker compose up -d storage-init node-exporter cadvisor storage-metrics prometheus grafana
./scripts/audit-phase-b.sh
```

Open Grafana at `http://192.168.1.10:3000` only when the LAN bind is enabled.
The provisioned `Trading Coin` folder contains Host, Container, Collector, and
Storage dashboards. Change the local Grafana password in `.env`; never commit it.

## Alerts and thresholds

- disconnected or target down: 2 minutes avoids short network transients;
- stale RAW write: older than 120 seconds for 2 minutes;
- queue depth: above 100 for 2 minutes;
- memory available: below 10% for 5 minutes;
- disk/inode warning: 70%; disk critical: 85%;
- write/quarantine counters: any increase in five minutes.

Alerts appear in Prometheus and Grafana only. Email, chat, and webhook delivery
require separate owner approval. Validate rule firing and resolution without
stopping services using the checked-in `promtool test rules` fixture.

## Troubleshooting

```bash
docker compose ps -a
docker compose logs --since=30m --no-color prometheus grafana node-exporter cadvisor
docker compose restart prometheus grafana
./scripts/audit-phase-b.sh
```

Restart only the affected monitoring service. Do not restart collector or
PostgreSQL for a dashboard issue. If Prometheus storage is unhealthy, preserve
`/data/coin-platform/prometheus` and inspect logs before any recovery action.

## Rollback

Record the current commit and health, then stop only monitoring services:

```bash
docker compose stop grafana prometheus storage-metrics cadvisor node-exporter
git revert <phase-b-commit>
docker compose config --quiet
```

Do not remove persistent monitoring directories without explicit approval.
