#!/bin/sh

set -eu

dump_path="${1:?Usage: replace-postgres-database.sh DUMP_PATH DATABASE MODE}"
database="${2:?Usage: replace-postgres-database.sh DUMP_PATH DATABASE MODE}"
mode="${3:?MODE must be rollback or migration}"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

: "${PGHOST:?Set PGHOST to the target PostgreSQL server}"
: "${PGUSER:?Set PGUSER to the target PostgreSQL administrator}"
: "${PGPASSWORD:?Set PGPASSWORD to the administrator password}"

if [ -d "$dump_path" ]; then
  sh "$script_directory/verify-postgres-dump.sh" "$dump_path"
  dump_file="$(find "$dump_path" -type f -name database.dump -print -quit)"
elif [ -f "$dump_path" ]; then
  dump_file="$dump_path"
else
  echo "PostgreSQL dump path does not exist: $dump_path" >&2
  exit 1
fi

case "$database" in
  postgres|template0|template1)
    echo "Refusing to replace PostgreSQL system database: $database" >&2
    exit 2
    ;;
  ''|*[!A-Za-z0-9_]* )
    echo "DATABASE may contain only letters, digits, and underscores." >&2
    exit 2
    ;;
esac

if [ "${REPLACE_DATABASE:-}" != "$database" ]; then
  echo "Set REPLACE_DATABASE=$database to confirm destructive replacement." >&2
  exit 2
fi

case "$mode" in
  rollback)
    ;;
  migration)
    : "${TARGET_APP_USER:?Set TARGET_APP_USER for migration mode}"
    case "$TARGET_APP_USER" in
      ''|*[!A-Za-z0-9_]* )
        echo "TARGET_APP_USER may contain only letters, digits, and underscores." >&2
        exit 2
        ;;
    esac
    ;;
  *)
    echo "MODE must be rollback or migration." >&2
    exit 2
    ;;
esac

echo "Disconnecting clients and replacing database $database on $PGHOST."
dropdb --maintenance-db=postgres --if-exists --force "$database"
createdb --maintenance-db=postgres "$database"

if [ "$mode" = "rollback" ]; then
  pg_restore --exit-on-error --dbname "$database" "$dump_file"
else
  pg_restore \
    --exit-on-error \
    --dbname "$database" \
    --no-owner \
    --no-privileges \
    "$dump_file"

  psql \
    --dbname "$database" \
    --set=database="$database" \
    --set=app_user="$TARGET_APP_USER" <<'SQL'
\set ON_ERROR_STOP on
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') AS exists \gset role_
\if :role_exists
\else
  \echo Target application role does not exist: :app_user
  \quit 1
\endif
GRANT CONNECT ON DATABASE :"database" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"app_user";
SQL
fi

table_count="$(psql --dbname "$database" -Atqc \
  "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');")"

if [ "$table_count" -eq 0 ]; then
  echo "Database replacement completed, but no application tables were found." >&2
  exit 1
fi

echo "Database $database replaced successfully with $table_count application table(s)."