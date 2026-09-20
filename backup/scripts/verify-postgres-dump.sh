#!/bin/sh

set -eu

search_directory="${1:?Usage: verify-postgres-dump.sh SEARCH_DIRECTORY}"

if [ ! -d "$search_directory" ]; then
  echo "Restore directory does not exist: $search_directory" >&2
  exit 1
fi

dumps="$(find "$search_directory" -type f -name database.dump)"
dump_count="$(printf "%s\n" "$dumps" | sed '/^$/d' | wc -l)"

if [ "$dump_count" -ne 1 ]; then
  echo "Expected one database.dump; found $dump_count." >&2
  exit 1
fi

dump_file="$(printf "%s\n" "$dumps" | sed -n '1p')"
dump_directory="$(dirname "$dump_file")"
checksum_file="$dump_directory/checksums.sha256"

if [ ! -f "$checksum_file" ]; then
  echo "Missing checksums.sha256 beside database.dump." >&2
  exit 1
fi

cd "$dump_directory"
sha256sum -c checksums.sha256

echo "PostgreSQL dump checksums verified."