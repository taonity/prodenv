#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
: "${FULLSTACK_STARTER_CHECKOUT:?Set FULLSTACK_STARTER_CHECKOUT to the fullstack-starter checkout}"
export PYTHONDONTWRITEBYTECODE=1
if [ -z "${AGE_EXECUTABLE:-}" ] || [ -z "${DOPPLER_EXECUTABLE:-}" ]; then
  tools="$(mktemp -d)"
  trap 'rm -rf "$tools"' EXIT
  sh credentials/doppler/install-tools.sh "$tools"
  export AGE_EXECUTABLE="$tools/age"
  export DOPPLER_EXECUTABLE="$tools/doppler"
fi
sh -n credentials/doppler/install.sh
sh -n credentials/doppler/install-tools.sh
sh -n backup/scripts/validate-secret-recovery.sh
sh -n backup/scripts/record-backup-metric.sh
sh -n "$FULLSTACK_STARTER_CHECKOUT/templates/docker/backup/export-postgres.sh"
python3 -m unittest discover -s credentials/doppler -p 'test_pilot.py'
node test/doppler-pilot.cjs "$FULLSTACK_STARTER_CHECKOUT"
python3 - <<'PY' | docker run --rm -i --entrypoint promtool prom/prometheus:v2.47.1 check metrics
import sys
sys.path.insert(0, 'credentials/doppler')
import pilot
from test_pilot import fixture_config
print(pilot.inventory_metrics(fixture_config()), end='')
PY
if [ -n "${DOCKER_WEBHOOK_CHECKOUT:-}" ]; then
  bash "$DOCKER_WEBHOOK_CHECKOUT/test/test-restart-project.sh"
fi
docker run --rm --entrypoint promtool -v "$PWD:/work:ro" -w /work \
  prom/prometheus:v2.47.1 test rules test/credential-rules.test.yml
docker run --rm --entrypoint promtool -v "$PWD/logging/prometheus:/etc/prometheus:ro" \
  prom/prometheus:v2.47.1 check config /etc/prometheus/prometheus.yml