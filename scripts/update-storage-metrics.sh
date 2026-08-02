#!/bin/sh
set -eu

while true; do
  output=/textfile/coin_storage.prom.tmp
  final=/textfile/coin_storage.prom
  {
    echo '# HELP coin_platform_storage_bytes Host storage used by platform area.'
    echo '# TYPE coin_platform_storage_bytes gauge'
    for area in bronze quarantine postgres prometheus grafana silver silver-quarantine; do
      bytes=$(du -sb "/coin-data/$area" 2>/dev/null | awk '{print $1}') || bytes=0
      printf 'coin_platform_storage_bytes{area="%s"} %s\n' "$area" "${bytes:-0}"
    done
    echo '# HELP coin_platform_storage_metrics_timestamp_seconds Storage scan completion time.'
    echo '# TYPE coin_platform_storage_metrics_timestamp_seconds gauge'
    printf 'coin_platform_storage_metrics_timestamp_seconds %s\n' "$(date +%s)"
  } > "$output"
  mv "$output" "$final"
  sleep 60
done
