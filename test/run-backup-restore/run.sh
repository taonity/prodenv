#!/bin/bash

set -e

cd "$(dirname $0)"

. ../util.sh

secrets_dir="backup/backrest/secrets"
fresh_dir=""
cleanup() {
    rm -f "$secrets_dir/admin_password" "$secrets_dir/repository_password" "$secrets_dir/aws_credentials"
    if [ -n "$fresh_dir" ]; then
        rm -rf "$fresh_dir"
    fi
}
trap cleanup EXIT

mkdir -p "$secrets_dir"
printf 'test-admin-password' > "$secrets_dir/admin_password"
printf 'test-repository-password' > "$secrets_dir/repository_password"
printf '[default]\naws_access_key_id = test\naws_secret_access_key = test\n' > "$secrets_dir/aws_credentials"

export BACKREST_INSTANCE=backup-integration-test
export BACKREST_REPOSITORY_ID=local-test
export BACKREST_REPOSITORY_URI=/tmp/backrest-test-repository
export BACKREST_ADMIN_USERNAME=admin

docker compose up -d --quiet-pull

compose_project_service_list=("portainer" "minio" "docker-webhook" "loki" "promtail" "grafana" "backrest" "node-exporter" "prometheus")

for service in "${compose_project_service_list[@]}"; do
    container_id="$(docker compose ps -q "$service")"
    if [ -z "$container_id" ] || [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" != "true" ]; then
        docker compose logs $service
        fail "Service $service is not running."
    fi
done

bootstrap_id="$(docker compose ps -a -q backrest-bootstrap)"
for attempt in $(seq 1 60); do
    bootstrap_state="$(docker inspect --format '{{.State.Status}}' "$bootstrap_id")"
    if [ "$bootstrap_state" = "exited" ]; then
        bootstrap_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$bootstrap_id")"
        if [ "$bootstrap_exit_code" != "0" ]; then
            docker compose logs backrest-bootstrap
            fail "Backrest bootstrap failed with exit code $bootstrap_exit_code."
        fi
        break
    fi
    if [ "$attempt" = "60" ]; then
        docker compose logs backrest-bootstrap
        fail "Backrest bootstrap did not complete."
    fi
    sleep 2
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
for project in project-a project-b; do
    mkdir -p "$fresh_dir/$project/postgres"
    printf 'test backup\n' > "$fresh_dir/$project/postgres/database.dump"
    date +%s > "$fresh_dir/$project/.last-success"
done
bash backup/scripts/validate-staging.sh "$fresh_dir" 1

printf '%s\n' "$(( $(date +%s) - 7200 ))" > "$fresh_dir/project-b/.last-success"
if bash backup/scripts/validate-staging.sh "$fresh_dir" 1; then
    fail "Stale exports must be rejected."
fi

rm "$fresh_dir/project-b/postgres/database.dump"
date +%s > "$fresh_dir/project-b/.last-success"
if bash backup/scripts/validate-staging.sh "$fresh_dir" 1; then
    fail "Empty exports must be rejected."
fi