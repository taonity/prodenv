#!/bin/sh

set -eu

operation="${1:?Usage: record-backup-metric.sh snapshot|integrity}"
metrics_directory="${METRICS_DIRECTORY:-/metrics}"

case "$operation" in
  snapshot|integrity) ;;
  *) echo "Unsupported backup metric operation." >&2; exit 2 ;;
esac

umask 022
mkdir -p "$metrics_directory"
temporary="$(mktemp "$metrics_directory/.backup-$operation.XXXXXX")"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
{
  printf '# HELP prodenv_backup_last_success_timestamp_seconds Last successful backup operation, as Unix seconds.\n'
  printf '# TYPE prodenv_backup_last_success_timestamp_seconds gauge\n'
  printf 'prodenv_backup_last_success_timestamp_seconds{operation="%s"} %s\n' "$operation" "$(date +%s)"
} > "$temporary"
chmod 644 "$temporary"
mv "$temporary" "$metrics_directory/backup-$operation.prom"