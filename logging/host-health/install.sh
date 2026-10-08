#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer as root on the Linux VPS.' >&2
  exit 1
fi
directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repository="$(CDPATH= cd -- "$directory/../.." && pwd)"
command -v apt-get >/dev/null
command -v docker >/dev/null
command -v systemctl >/dev/null
docker volume inspect prodenv-monitoring-textfile >/dev/null
apt-get update
apt-get install -y python3 sysbench fio iputils-ping curl openssl
if grep -q '^ID=ubuntu$' /etc/os-release; then
  apt-get install -y update-notifier-common
fi
install -d -m 755 /opt/prodenv-health
install -m 644 "$directory/collect.py" "$directory/run.sh" /opt/prodenv-health/
if [ ! -e /etc/prodenv-health.json ]; then
  python3 - "$directory/config.json" "$repository" <<'PY'
import json
from pathlib import Path
import sys
config = json.loads(Path(sys.argv[1]).read_text())
for certificate in config["certificates"]:
    certificate["path"] = certificate["path"].replace("@REPO@", sys.argv[2])
Path("/etc/prodenv-health.json").write_text(json.dumps(config, indent=2) + "\n")
PY
fi
install -m 644 "$directory/prodenv-health.service" "$directory/prodenv-health.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now prodenv-health.timer
echo 'Timer enabled. Configuration: /etc/prodenv-health.json'
echo 'Benchmarks calibrate for at least seven days. No provider guarantee is inferred.'