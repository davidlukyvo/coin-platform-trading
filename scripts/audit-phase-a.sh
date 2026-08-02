#!/usr/bin/env bash
set -uo pipefail

# Read-only Phase A audit. It intentionally does not source or print .env.
repo_dir="${1:-$HOME/coin-platform-trading}"
data_root="${COIN_DATA_ROOT:-/data/coin-platform}"

if [[ ! -d "$repo_dir" ]]; then
  printf 'ERROR repository not found: %s\n' "$repo_dir" >&2
  exit 2
fi

cd "$repo_dir" || exit 2

echo '===== TIME ====='
date -Is
uptime
timedatectl show -p Timezone -p NTPSynchronized --value

echo '===== GIT ====='
printf 'branch='; git branch --show-current
printf 'commit='; git rev-parse HEAD
printf 'dirty_files='; git status --porcelain | wc -l

echo '===== HOST ====='
free -h
swapon --show
df -hT / "$data_root"
df -hi / "$data_root"
findmnt "$data_root" || true

echo '===== COMPOSE ====='
docker compose ps -a

echo '===== RESTART_COUNTS ====='
while IFS= read -r container_id; do
  [[ -z "$container_id" ]] && continue
  docker inspect "$container_id" \
    --format '{{.Name}} status={{.State.Status}} restart={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}'
done < <(docker compose ps -aq)

echo '===== HEALTH ====='
curl -fsS http://127.0.0.1:8000/healthz || true
echo

echo '===== METRICS ====='
curl -fsS http://127.0.0.1:8000/metrics 2>/dev/null |
  grep -E '^coin_collector_(connected|events_total|errors_total|reconnects_total|write_queue_depth|quarantined_total|last_write_unixtime)' || true

echo '===== RAW ====='
du -sh "$data_root/bronze" "$data_root/quarantine" 2>/dev/null || true
printf 'raw_files='; find "$data_root/bronze" -type f -name '*.ndjson' 2>/dev/null | wc -l
printf 'quarantine_files='; find "$data_root/quarantine" -type f 2>/dev/null | wc -l
find "$data_root/bronze" -type f -name '*.ndjson' \
  -printf '%TY-%Tm-%TdT%TH:%TM:%TS %s %p\n' 2>/dev/null | sort | tail -20

echo '===== RECENT_ERRORS ====='
docker compose logs --since=2h --no-color collector 2>&1 |
  grep -Ei 'warn|error|exception|fatal|quarantine|reconnect|failed' | tail -100 || true
