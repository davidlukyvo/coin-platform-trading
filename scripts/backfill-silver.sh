#!/usr/bin/env bash
set -euo pipefail
cd "${1:-$HOME/coin-platform-trading}"
start_date="${2:?start date YYYY-MM-DD is required}"
end_date="${3:?end date YYYY-MM-DD is required}"
mode="${4:---dry-run}"

case "$mode" in
  --dry-run|--validate-only) ;;
  --write-without-checkpoint) mode="" ;;
  --write-and-commit-checkpoint) mode="--backfill-commit-checkpoint" ;;
  *) echo "invalid mode" >&2; exit 2 ;;
esac

docker compose run --rm -T --no-deps silver-processor python app.py --once \
  --backfill-start "$start_date" --backfill-end "$end_date" $mode
