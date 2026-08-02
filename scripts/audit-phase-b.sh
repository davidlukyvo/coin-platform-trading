#!/usr/bin/env bash
set -uo pipefail
cd "${1:-$HOME/coin-platform-trading}" || exit 2

echo '===== COMPOSE ====='
docker compose ps -a
echo '===== HEALTH ====='
curl -fsS http://127.0.0.1:8000/healthz; echo
echo '===== TARGETS ====='
prometheus_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' coin-platform-prometheus-1)
targets_json=$(curl -fsS "http://${prometheus_ip}:9090/api/v1/targets?state=active")
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); [print(t["labels"].get("job"), t["health"], t.get("lastError", "")) for t in d["data"]["activeTargets"]]' "$targets_json"
echo '===== RULES ====='
rules_json=$(curl -fsS "http://${prometheus_ip}:9090/api/v1/rules")
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); r=[x for g in d["data"]["groups"] for x in g["rules"]]; print("rules",len(r),"firing",sum(x.get("state")=="firing" for x in r),"unhealthy",sum(x.get("health")!="ok" for x in r))' "$rules_json"
echo '===== METRICS ====='
for query in coin_collector_connected node_uname_info container_last_seen coin_platform_storage_bytes; do
  result_json=$(curl -fsS "http://${prometheus_ip}:9090/api/v1/query?query=${query}")
  count=$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["data"]["result"]))' "$result_json")
  printf '%s=%s series\n' "$query" "$count"
done
echo '===== STORAGE ====='
du -sh /data/coin-platform/{bronze,postgres,prometheus,grafana} 2>/dev/null || true
df -hT /data
echo '===== COLLECTOR_GUARD ====='
for service in collector postgres; do
  cid=$(docker compose ps -q "$service")
  docker inspect "$cid" --format '{{.Name}} restart={{.RestartCount}} health={{.State.Health.Status}}'
done
