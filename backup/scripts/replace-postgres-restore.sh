#!/bin/sh

set -eu

restore_directory="${1:?Usage: replace-postgres-restore.sh RESTORE_DIRECTORY DATABASE MODE}"
database="${2:?Usage: replace-postgres-restore.sh RESTORE_DIRECTORY DATABASE MODE}"
mode="${3:?MODE must be rollback or migration}"
restore_volume="${RESTORE_VOLUME:-prodenv-backup-restore}"
postgres_image="${POSTGRES_IMAGE:-postgres:16}"
postgres_port="${PGPORT:-5432}"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

: "${DOCKER_NETWORK:?Set DOCKER_NETWORK to the target Docker network}"
: "${PGHOST:?Set PGHOST to the target PostgreSQL service name}"
: "${PGUSER:?Set PGUSER to the target PostgreSQL administrator}"
: "${PGPASSWORD:?Set PGPASSWORD to the administrator password}"

case "$restore_directory" in
  /*|*..*|*[!A-Za-z0-9._/-]*)
    echo "RESTORE_DIRECTORY must be a relative path without '..'." >&2
    exit 2
    ;;
esac

RESTORE_VOLUME="$restore_volume" POSTGRES_IMAGE="$postgres_image" \
  sh "$script_directory/verify-postgres-restore.sh" "$restore_directory"

docker run --rm \
  --network "$DOCKER_NETWORK" \
  -e PGHOST="$PGHOST" \
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