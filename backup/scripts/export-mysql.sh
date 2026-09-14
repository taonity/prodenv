#!/bin/sh

set -eu

: "${BACKUP_PROJECT:?Set BACKUP_PROJECT to a stable project name}"
: "${MYSQL_DATABASE:?Set MYSQL_DATABASE to the database to export}"

case "$BACKUP_PROJECT" in
  *[!A-Za-z0-9._-]*)
    echo "BACKUP_PROJECT may contain only letters, digits, dot, underscore, and dash." >&2
    exit 2
    ;;
esac

if command -v mariadb-dump >/dev/null 2>&1; then
  dump_command=mariadb-dump
elif command -v mysqldump >/dev/null 2>&1; then
  dump_command=mysqldump
else
  echo "mariadb-dump or mysqldump is required." >&2
  exit 1
fi

umask 077
staging_root="${BACKUP_STAGING_DIR:-/backup-staging}"
project_dir="$staging_root/$BACKUP_PROJECT"
current_dir="$project_dir/mysql"
previous_dir="$project_dir/mysql.previous"
lock_dir="$project_dir/.export-in-progress"
temp_dir="$project_dir/.export-mysql-$$"

mkdir -p "$project_dir"
if ! mkdir "$lock_dir"; then
  echo "Another export is running or $lock_dir is stale." >&2
  exit 1
fi

cleanup() {
  rm -rf "$temp_dir" "$lock_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$temp_dir"
"$dump_command" \
  --host="${MYSQL_HOST:-mysql}" \
  --port="${MYSQL_PORT:-3306}" \
  --user="${MYSQL_USER:-root}" \
  --single-transaction \
  --routines \
  --events \
  --triggers \
  --hex-blob \
  --databases "$MYSQL_DATABASE" > "$temp_dir/database.sql"
{
  printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'database=%s\n' "$MYSQL_DATABASE"
  "$dump_command" --version
} > "$temp_dir/metadata.txt"
(cd "$temp_dir" && sha256sum database.sql) > "$temp_dir/checksums.sha256"

rm -rf "$previous_dir"
if [ -d "$current_dir" ]; then
  mv "$current_dir" "$previous_dir"
fi
mv "$temp_dir" "$current_dir"
rm -rf "$previous_dir"
date +%s > "$project_dir/.last-success.tmp"
mv "$project_dir/.last-success.tmp" "$project_dir/.last-success"

echo "MySQL export completed for $BACKUP_PROJECT."