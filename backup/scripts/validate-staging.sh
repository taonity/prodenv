#!/bin/sh

set -eu

staging_root="${1:?Usage: validate-staging.sh STAGING_ROOT [MAX_AGE_HOURS]}"
max_age_hours="${2:-26}"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_count=0

if [ ! -d "$staging_root" ]; then
  echo "Staging directory does not exist: $staging_root" >&2
  exit 1
fi

for project_dir in "$staging_root"/*; do
  if [ ! -d "$project_dir" ]; then
    continue
  fi
  project_count=$((project_count + 1))
  sh "$script_dir/validate-export.sh" "$project_dir" "$max_age_hours"
done

if [ "$project_count" -eq 0 ]; then
  echo "No project exports found in $staging_root" >&2
  exit 1
fi

echo "Validated $project_count staged project export(s)."