#!/bin/sh
set -eu
metrics_directory="$(docker volume inspect prodenv-monitoring-textfile --format '{{.Mountpoint}}')"
exec python3 /opt/prodenv-health/collect.py --metrics-directory "$metrics_directory"