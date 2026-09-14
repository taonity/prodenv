#!/bin/bash

set -e

cd "$(dirname $0)"

. ../util.sh


docker compose up -d --quiet-pull

compose_project_service_list=("portainer" "minio" "docker-webhook" "loki" "promtail" "grafana" "backrest" "node-exporter" "prometheus")

for service in "${compose_project_service_list[@]}"; do
    container_id="$(docker compose ps -q "$service")"
    if [ -z "$container_id" ] || [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" != "true" ]; then
        docker compose logs $service
        fail "Service $service is not running."
    fi
done

backrest_id="$(docker compose ps -q backrest)"
for attempt in $(seq 1 30); do
    if [ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$backrest_id")" = "healthy" ]; then
        break
    fi
    if [ "$attempt" = "30" ]; then
        docker compose logs backrest
        fail "Backrest did not become healthy."
    fi
    sleep 2
done

userdata_writable="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/userdata"}}{{.RW}}{{end}}{{end}}' "$backrest_id")"
if [ "$userdata_writable" != "false" ]; then
    fail "Backrest staging mount must be read-only."
fi

fresh_dir="$(mktemp -d)"
trap 'rm -rf "$fresh_dir"' EXIT
mkdir -p "$fresh_dir/postgres"
printf 'test backup\n' > "$fresh_dir/postgres/database.dump"
date +%s > "$fresh_dir/.last-success"
bash backup/scripts/validate-export.sh "$fresh_dir" 1

printf '%s\n' "$(( $(date +%s) - 7200 ))" > "$fresh_dir/.last-success"
if bash backup/scripts/validate-export.sh "$fresh_dir" 1; then
    fail "Stale exports must be rejected."
fi

rm "$fresh_dir/postgres/database.dump"
date +%s > "$fresh_dir/.last-success"
if bash backup/scripts/validate-export.sh "$fresh_dir" 1; then
    fail "Empty exports must be rejected."
fi
