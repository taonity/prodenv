#!/bin/sh

set -eu

dump_file="${1:?Usage: import-postgres-dump.sh DUMP_FILE [DATABASE]}"
database="${2:-restore_test}"

if [ ! -f "$dump_file" ]; then
  echo "PostgreSQL dump does not exist: $dump_file" >&2
  exit 1
fi

pg_restore \
  --exit-on-error \
  --username postgres \
  --dbname "$database" \
  --no-owner \
  --no-privileges \
  "$dump_file"

table_count="$(psql -U postgres -d "$database" -Atqc \
  "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');")"

if [ "$table_count" -eq 0 ]; then
  echo "Restore completed, but no application tables were found." >&2
  exit 1
fi

echo "Database import verified: $table_count application table(s) found."