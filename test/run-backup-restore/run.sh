#!/bin/bash

set -e

cd "$(dirname $0)"

. ../util.sh


docker compose up -d --quiet-pull

# Just to make sure nothing will fall during 5 sec
sleep 5

compose_project_service_list=("portainer" "minio" "mc" "restore-backup" "webhook" "loki" "promtail" "grafana" "make-backup")

for service in "${compose_project_service_list[@]}"; do
    if [ -z `docker-compose ps -q $service` ] || [ -z `docker ps -q --no-trunc | grep $(docker-compose ps -q $service)` ]; then
        docker compose logs $service
        fail "Service $service is not running."
    fi
done

backup_making_logs=$(backup/make-backup/scripts/make.sh)
if [[ $backup_making_logs != *"container run-backup-restore-mc-1"* ]]; then
    echo $backup_making_logs
    fail "Failed to backup mc."
fi

if [[ $backup_making_logs != *"Finished running backup tasks."* ]]; then
    echo $backup_making_logs
    fail "Failed to to finish backup making."
fi

backup_restoring_logs=$(backup/restore-backup/scripts/restore.sh backup.latest.tar.gz)
if [[ $backup_restoring_logs != *"Finish execution"* ]]; then
    echo $backup_restoring_logs
    fail "Failed to finish backup restoring."
fi
