#!/bin/sh

set -eu

restore_directory="${1:?Usage: verify-postgres-restore.sh RESTORE_DIRECTORY}"
restore_volume="${RESTORE_VOLUME:-prodenv-backup-restore}"
postgres_image="${POSTGRES_IMAGE:-postgres:16}"
script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

case "$restore_directory" in
  /*|*..*|*[!A-Za-z0-9._/-]*)
    echo "RESTORE_DIRECTORY must be a relative path without '..'." >&2
    exit 2
    ;;
esac

if ! docker volume inspect "$restore_volume" >/dev/null 2>&1; then
  echo "Docker volume does not exist: $restore_volume" >&2
  exit 1
fi

docker run --rm \
  --network none \
  -v "$restore_volume:/restore:ro" \
  -v "$script_directory:/backup-scripts:ro" \
  "$postgres_image" \
  sh /backup-scripts/verify-postgres-dump.sh \
  "/restore/$restore_directory"

echo "Restore checksums verified successfully."