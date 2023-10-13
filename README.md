# prodenv

This is a production environment for docker applications. It contains logging, deploying, backingup, and some UI tools.

### Tool list
 - Continuous Delivery
   - [generaltao725/docker-webhook](https://hub.docker.com/r/generaltao725/docker-webhook) - updates application on DockerHub update
 - Logging
   - [grafana/grafana](https://hub.docker.com/r/grafana/grafana) - the UI for showing logs
   - [grafana/loki](https://hub.docker.com/r/grafana/loki) - like Prometheus, but for logs
   - [grafana/promtail](https://hub.docker.com/r/grafana/promtail) - the agent, responsible for gathering logs and sending them to Loki
   - [prom/prometheus](https://hub.docker.com/r/prom/prometheus) - server monitoring system
   - [prom/node-exporter](https://hub.docker.com/r/prom/node-exporter) - prometheus exporter for metrics
   - [minio/minio](https://hub.docker.com/r/minio/minio) - the db for logs
   - [minio/mc](https://hub.docker.com/r/minio/mc) - the client for minio, required for backups
 - Backup
   - [offen/docker-volume-backup](https://hub.docker.com/r/offen/docker-volume-backup) - making backups
   - [generaltao725/command-runner](https://hub.docker.com/r/generaltao725/command-runner) - restoring backups
 - [portainer/portainer](https://hub.docker.com/r/portainer/portainer) - the UI for Docker
 - [jc21/nginx-proxy-manager](https://hub.docker.com/r/jc21/nginx-proxy-manager) - nginx with UI for reverse proxy server management

### Usage
All sensitive information is removed for security purposes, this repo is more for review.
