#!/bin/sh

set -eu

restore_directory="${1:?Usage: validate-postgres-restore.sh RESTORE_DIRECTORY}"
restore_volume="${RESTORE_VOLUME:-prodenv-backup-restore}"
postgres_image="${POSTGRES_IMAGE:-postgres:16}"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
container="backrest-restore-validation-$(date +%s)-$$"
database="restore_validation"

RESTORE_VOLUME="$restore_volume" POSTGRES_IMAGE="$postgres_image" \
  sh "$script_directory/verify-postgres-restore.sh" "$restore_directory"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker run -d \
  --name "$container" \
  --network none \
  -e POSTGRES_PASSWORD=restore-validation-only \
  -e POSTGRES_DB="$database" \
  -v "$restore_volume:/restore:ro" \
  -v "$script_directory:/backup-scripts:ro" \
  "$postgres_image" >/dev/null

attempt=0
until docker exec "$container" pg_isready -U postgres -d "$database" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    docker logs "$container" >&2
    echo "PostgreSQL validation container did not become ready." >&2
    exit 1
  fi
  sleep 1
done

restore_path="/restore/$restore_directory"
dump_file="$(docker exec "$container" find "$restore_path" -type f -name database.dump -print -quit)"

docker exec "$container" \
  sh /backup-scripts/import-postgres-dump.sh "$dump_file" "$database"

echo "PostgreSQL restore validation completed successfully."