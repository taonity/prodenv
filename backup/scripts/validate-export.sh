#!/bin/sh

set -eu

export_dir="${1:?Usage: validate-export.sh EXPORT_DIR [MAX_AGE_HOURS]}"
max_age_hours="${2:-26}"

case "$max_age_hours" in
  ''|*[!0-9]*)
    echo "MAX_AGE_HOURS must be a positive integer." >&2
    exit 2
    ;;
esac

if [ ! -d "$export_dir" ]; then
  echo "Export directory does not exist: $export_dir" >&2
  exit 1
fi

if [ -d "$export_dir/.export-in-progress" ]; then
  echo "An export is still in progress: $export_dir" >&2
  exit 1
fi

marker="$export_dir/.last-success"
if [ ! -f "$marker" ]; then
  echo "No successful export marker found in $export_dir" >&2
  exit 1
fi

export_time="$(cat "$marker")"
case "$export_time" in
  ''|*[!0-9]*)
    echo "Invalid export timestamp in $marker" >&2
    exit 1
    ;;
esac

age_seconds=$(( $(date +%s) - export_time ))
if [ "$age_seconds" -lt 0 ] || [ "$age_seconds" -gt $((max_age_hours * 3600)) ]; then
  echo "Export in $export_dir is stale or has an invalid future timestamp." >&2
  exit 1
fi

if ! find "$export_dir" -type f ! -name '.last-success' -size +0c -print -quit | grep -q .; then
  echo "No non-empty export files found in $export_dir" >&2
  exit 1
fi

echo "Export is complete and fresh: $export_dir"