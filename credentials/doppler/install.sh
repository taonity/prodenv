#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer as root on the Linux deployment host.' >&2
  exit 1
fi
: "${1:?Pass the fullstack-starter templates/docker/doppler-pilot.json template}"
command -v docker >/dev/null
command -v python3 >/dev/null
command -v systemctl >/dev/null
directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repository="$(CDPATH= cd -- "$directory/../.." && pwd)"
sh "$directory/install-tools.sh" /usr/local/bin
install -d -m 755 /opt/prodenv-doppler
install -d -m 700 /etc/prodenv-doppler /etc/prodenv-doppler/tokens
install -d -m 700 /var/lib/prodenv-doppler /var/lib/prodenv-secret-recovery
install -m 644 "$directory/pilot.py" "$directory/broker.py" /opt/prodenv-doppler/
install -m 644 "$directory/catalog.json" /opt/prodenv-doppler/catalog.json
install -m 644 "$directory/"*.service "$directory/"*.timer /etc/systemd/system/
python3 - "$1" "$repository" "$directory/recovery-plan.json" <<'PY'
import json
import os
from pathlib import Path
import sys
os.umask(0o077)
template = json.loads(Path(sys.argv[1]).read_text())
repository = Path(sys.argv[2])
for environment in ("stage", "prod"):
    deployment = "fullstack-starter-" + environment
    destination = Path("/etc/prodenv-doppler") / (deployment + ".json")
    if destination.exists():
        continue
    config = json.loads(json.dumps(template))
    config["deployment"] = deployment
    config["template_directory"] = "/etc/webhook/cache/" + deployment
    config["host_override"] = str(repository / "cicd/docker-webhook/shared/envs" / deployment / "docker-compose.override.yml")
    for spec in config["credentials"].values():
        spec["key"] = spec["key"].replace("FULLSTACK_STARTER_STAGE_", "FULLSTACK_STARTER_" + environment.upper() + "_")
    destination.write_text(json.dumps(config, indent=2) + "\n")
desired = json.loads((repository / "backup/backrest/desired.json").read_text())
plan = json.loads(Path(sys.argv[3]).read_text())
desired["plans"] = [entry for entry in desired["plans"] if entry["id"] != plan["id"]] + [plan]
Path("/etc/prodenv-doppler/backrest-desired.json").write_text(json.dumps(desired, indent=2) + "\n")
PY
systemctl daemon-reload
echo 'Installed without activation. Configure the host manifest and scoped token files outside the development workspace.'
echo 'Add an age PUBLIC recipient and set enabled=true only after the recovery private key and Backrest access material are stored off-server.'
echo 'Import namespaced sandbox secrets and install a read-only service token at /etc/prodenv-doppler/tokens/sandbox (root-owned mode 0600).'
echo 'Publish the updated Starter image and generaltao725/docker-webhook:0.10.0 before enabling managed deployment.'
echo 'Enable the broker, then use the base + ports + credentials/doppler/docker-compose.yml overlay.'
echo 'Pause pilot deployments during bootstrap. Existing database admin/app role names must match the manifest.'
echo 'Check with: python3 /opt/prodenv-doppler/pilot.py check /etc/prodenv-doppler/fullstack-starter-stage.json'
echo 'Bootstrap explicitly with: python3 /opt/prodenv-doppler/pilot.py bootstrap /etc/prodenv-doppler/fullstack-starter-stage.json --confirm-database-role-changes'
echo 'After success, enable the prodenv-doppler@ delivery service and prodenv-doppler-poll@ / prodenv-doppler-metrics@ timers for the selected deployment.'
echo 'Run backrest-bootstrap with the opt-in overlay to reconcile the independent secret-recovery backup plan.'
echo 'Offline recovery: pilot.py recover HOST_MANIFEST --snapshot SNAPSHOT.age --identity OFFLINE_KEY_FILE (cached application images and templates required).'