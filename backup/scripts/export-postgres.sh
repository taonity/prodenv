#!/bin/sh

set -eu

: "${BACKUP_PROJECT:?Set BACKUP_PROJECT to a stable project name}"
: "${PGDATABASE:?Set PGDATABASE to the database to export}"

case "$BACKUP_PROJECT" in
  *[!A-Za-z0-9._-]*)
    echo "BACKUP_PROJECT may contain only letters, digits, dot, underscore, and dash." >&2
    exit 2
    ;;
esac

umask 077
staging_root="${BACKUP_STAGING_DIR:-/backup-staging}"
project_dir="$staging_root/$BACKUP_PROJECT"
current_dir="$project_dir/postgres"
previous_dir="$project_dir/postgres.previous"
lock_dir="$project_dir/.export-in-progress"
temp_dir="$project_dir/.export-postgres-$$"

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
pg_dump --format=custom --file="$temp_dir/database.dump" "$PGDATABASE"
pg_dumpall --globals-only > "$temp_dir/globals.sql"
{
  printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'database=%s\n' "$PGDATABASE"
  pg_dump --version
} > "$temp_dir/metadata.txt"
(cd "$temp_dir" && sha256sum database.dump globals.sql) > "$temp_dir/checksums.sha256"

rm -rf "$previous_dir"
if [ -d "$current_dir" ]; then
  mv "$current_dir" "$previous_dir"
fi
mv "$temp_dir" "$current_dir"
rm -rf "$previous_dir"
date +%s > "$project_dir/.last-success.tmp"
mv "$project_dir/.last-success.tmp" "$project_dir/.last-success"

echo "PostgreSQL export completed for $BACKUP_PROJECT."