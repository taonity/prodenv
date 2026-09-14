#!/bin/sh

set -eu

event="${1:-backup event}"
message="${2:-Backrest operation completed}"

if [ -z "${NTFY_URL:-}" ]; then
  echo "NTFY_URL is not configured; notification skipped."
  exit 0
fi

set -- -fsS --retry 3 -H "Title: Backrest: $event" -d "$message"
if [ -n "${NTFY_TOKEN:-}" ]; then
  set -- "$@" -H "Authorization: Bearer $NTFY_TOKEN"
fi

curl "$@" "$NTFY_URL"