# Operations Runbook

This runbook operates the Phase A market-data stack on `trading-data-01`.
Run commands from `~/coin-platform-trading`. All timestamps and partitions are
UTC. Never print `.env`, remove `/data/coin-platform`, or use Compose volume
deletion commands during routine recovery.

## Start, stop, and status

```bash
docker compose up -d --build
docker compose stop
docker compose ps -a
```

`storage-init` should finish with exit code 0. `collector` and `postgres`
should be running and healthy. A planned stop does not delete containers or
host-mounted data.

## Health and metrics

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/metrics |
  grep -E '^coin_collector_(connected|events_total|errors_total|reconnects_total|write_queue_depth|quarantined_total|last_write_unixtime)'
```

Healthy acceptance values are `connected=1`, queue depth zero or short-lived,
fresh last-event/last-write timestamps, and no persistent write failure. Event
counters should increase for every configured symbol and stream.

## Check RAW data

```bash
du -sh /data/coin-platform/bronze
find /data/coin-platform/bronze -type f -name '*.ndjson' | wc -l
find /data/coin-platform/bronze -type f -name '*.ndjson' \
  -printf '%TY-%Tm-%TdT%TH:%TM:%TS %s %p\n' | sort | tail -20
find /data/coin-platform/quarantine -type f | wc -l
```

For a UTC rollover, expect one `events.ndjson` for each configured stream under
the new `date=YYYY-MM-DD/hour=00` partition. Do not edit existing RAW files.

## Check disk and inode capacity

```bash
df -hT / /data
df -hi / /data
findmnt /data
du -sh /data/coin-platform/{bronze,quarantine,postgres}
```

Investigate before `/data` reaches 80% usage. Do not delete RAW as an emergency
measure without owner approval and a verified backup or retention decision.

## Collector unhealthy

1. Capture evidence before changing runtime:

   ```bash
   docker compose ps -a
   curl -sS http://127.0.0.1:8000/healthz || true
   docker compose logs --since=30m --no-color collector | tail -200
   df -hT /data
   ```

2. Check for stale events, Binance connectivity, queue growth, disk-full or
   permission errors.
3. If the cause is understood and data writes are safe, restart only the
   collector: `docker compose restart collector`.
4. Re-run `./scripts/audit-phase-a.sh` and verify RAW resumes in the current UTC
   partition. Escalate repeated write/quarantine failures instead of looping
   restarts.

## PostgreSQL unhealthy

1. Capture `docker compose ps -a`, disk usage, and the last 200 PostgreSQL log
   lines.
2. Do not remove or recreate `/data/coin-platform/postgres`.
3. If disk and permissions are sound, restart only PostgreSQL:
   `docker compose restart postgres`.
4. Confirm `healthy` with `docker compose ps` and check logs for recovery or
   corruption warnings. Stop and escalate suspected corruption.

## Post-reboot validation

Reboot only with owner approval. After the VM returns:

```bash
findmnt /data
systemctl is-active docker
cd ~/coin-platform-trading
docker compose ps -a
./scripts/audit-phase-a.sh
```

Confirm the `COIN_DATA` filesystem is mounted before accepting writes, both
long-running services recovered, restart counts are understood, the current
partition is receiving data, and pre-reboot RAW remains present.

## Rollback

Before deployment, record the branch, commit, image IDs, Compose state, health,
and RAW size. To roll back source and images without touching data:

```bash
git switch feature/platform-bootstrap-v1
git pull --ff-only
git checkout <last-known-good-commit> -- .
docker compose config --quiet
docker compose up -d --build
./scripts/audit-phase-a.sh
```

Prefer reverting the faulty commit and committing the revert on the feature
branch. Never use `git reset --hard`, force-push, `docker compose down -v`, or
delete host data as a rollback shortcut.
