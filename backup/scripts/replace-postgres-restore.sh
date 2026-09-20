#!/bin/sh

set -eu

restore_directory="${1:?Usage: replace-postgres-restore.sh RESTORE_DIRECTORY DATABASE MODE}"
database="${2:?Usage: replace-postgres-restore.sh RESTORE_DIRECTORY DATABASE MODE}"
mode="${3:?MODE must be rollback or migration}"
restore_volume="${RESTORE_VOLUME:-prodenv-backup-restore}"
postgres_image="${POSTGRES_IMAGE:-postgres:16}"
postgres_port="${PGPORT:-5432}"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

: "${DB_CONTAINER:?Set DB_CONTAINER to the target PostgreSQL container name}"
: "${PGUSER:?Set PGUSER to the target PostgreSQL administrator}"
: "${PGPASSWORD:?Set PGPASSWORD to the administrator password}"

if [ "$(docker inspect --format '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null || true)" != "true" ]; then
  echo "PostgreSQL container is not running: $DB_CONTAINER" >&2
  exit 1
fi

case "$restore_directory" in
  /*|*..*|*[!A-Za-z0-9._/-]*)
    echo "RESTORE_DIRECTORY must be a relative path without '..'." >&2
    exit 2
    ;;
esac

RESTORE_VOLUME="$restore_volume" POSTGRES_IMAGE="$postgres_image" \
  sh "$script_directory/verify-postgres-restore.sh" "$restore_directory"

docker run --rm \
  --network "container:$DB_CONTAINER" \
  -e PGHOST=127.0.0.1 \
  -e PGPORT="$postgres_port" \
  -e PGUSER="$PGUSER" \
  -e PGPASSWORD="$PGPASSWORD" \
  -e REPLACE_DATABASE="${REPLACE_DATABASE:-}" \
  -e TARGET_APP_USER="${TARGET_APP_USER:-}" \
  -v "$restore_volume:/restore:ro" \
  -v "$script_directory:/backup-scripts:ro" \
  "$postgres_image" \
  sh /backup-scripts/replace-postgres-database.sh \
  "/restore/$restore_directory" "$database" "$mode"