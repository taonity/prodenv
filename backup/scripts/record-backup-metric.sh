#!/bin/sh

set -eu

operation="${1:?Usage: record-backup-metric.sh snapshot|integrity|restore [RESTORE_DIRECTORY]}"
metrics_directory="${METRICS_DIRECTORY:-/metrics}"

case "$operation" in
  snapshot|integrity) ;;
  restore)
    restore_directory="${2:?RESTORE_DIRECTORY is required}"
    case "$restore_directory" in
      /*|*..*|*[!A-Za-z0-9._/-]*) echo 'Invalid restore directory.' >&2; exit 2 ;;
    esac
    ;;
  *) echo "Unsupported backup metric operation." >&2; exit 2 ;;
esac

umask 022
mkdir -p "$metrics_directory"
temporary="$(mktemp "$metrics_directory/.backup-$operation.XXXXXX")"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
{
  printf '# HELP prodenv_backup_last_success_timestamp_seconds Last successful backup operation, as Unix seconds.\n'
  printf '# TYPE prodenv_backup_last_success_timestamp_seconds gauge\n'
  if [ "$operation" = restore ]; then
    printf 'prodenv_backup_last_success_timestamp_seconds{operation="restore",scope="%s"} %s\n' "$restore_directory" "$(date +%s)"
  else
    printf 'prodenv_backup_last_success_timestamp_seconds{operation="%s"} %s\n' "$operation" "$(date +%s)"
  fi
} > "$temporary"
chmod 644 "$temporary"
mv "$temporary" "$metrics_directory/backup-$operation.prom"