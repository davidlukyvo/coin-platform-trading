#!/usr/bin/env bash
set -euo pipefail
cd "${1:-$HOME/coin-platform-trading}"

echo '===== SERVICES ====='
docker compose ps -a
for service in collector postgres silver-processor; do
  cid=$(docker compose ps -q "$service")
  docker inspect "$cid" --format '{{.Name}} restart={{.RestartCount}} health={{.State.Health.Status}} started={{.State.StartedAt}}'
done
echo '===== SILVER_HEALTH ====='
silver_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' coin-platform-silver-processor-1)
curl -fsS "http://${silver_ip}:8010/healthz"; echo
echo '===== SILVER_METRICS ====='
curl -fsS "http://${silver_ip}:8010/metrics" | grep -E '^silver_(records_|duplicates|batches|lag|last_success|checkpoint|files_written|write_errors)'
echo '===== DATA ====='
du -sh /data/coin-platform/{bronze,silver,silver-quarantine,checkpoints/silver,reports/silver}
printf 'parquet_files='; find /data/coin-platform/silver -type f -name '*.parquet' | wc -l
printf 'rejected_files='; find /data/coin-platform/silver-quarantine -type f | wc -l
./scripts/validate-silver.sh "$PWD"
echo '===== COLLECTOR ====='
curl -fsS http://127.0.0.1:8000/healthz; echo
