# prodenv

This repository contains a production environment for Docker applications, including logging, deployment, backup, and administration tools.

### Tool list

- **Continuous delivery**
  - [docker-webhook](https://hub.docker.com/r/generaltao725/docker-webhook) — updates applications when Docker Hub images change
- **Logging and monitoring**
  - [Grafana](https://hub.docker.com/r/grafana/grafana) — log and metrics dashboards
  - [Loki](https://hub.docker.com/r/grafana/loki) — log aggregation
  - [Promtail](https://hub.docker.com/r/grafana/promtail) — collects and forwards logs to Loki
  - [Prometheus](https://hub.docker.com/r/prom/prometheus) — monitoring and metrics
  - [Node Exporter](https://hub.docker.com/r/prom/node-exporter) — host metrics exporter
  - [MinIO](https://hub.docker.com/r/minio/minio) — object storage for logs
- **Backup**
  - [Backrest](https://github.com/garethgeorge/backrest) — backup UI, scheduling, retention, and health operations
  - [restic](https://restic.net/) — encrypted, deduplicated off-site snapshots
  - Database-native logical exports — consistent, portable database backups
- [Portainer](https://hub.docker.com/r/portainer/portainer) — Docker UI
- [NGINX](https://hub.docker.com/_/nginx) — reverse proxy

### Usage

Sensitive information has been removed; this repository is intended for review.

See [backup/README.md](backup/README.md) for backup setup, project integration, retention, restore testing, and server migration procedures.

### Notes

#### Docker log retention

Configure automatic Docker log retention. See the [Docker JSON-file logging documentation](https://docs.docker.com/engine/logging/drivers/json-file/).

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

#### NGINX certificate renewal

Run every six hours to renew certificates and reload NGINX:

```cron
0 */6 * * * docker compose -f /path/to/docker-compose.yml up certbot && docker compose -f /path/to/docker-compose.yml exec nginx nginx -s reload
```

TODO: consider an NGINX module for certificate generation.
