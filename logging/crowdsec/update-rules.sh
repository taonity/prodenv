#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
docker compose exec -T crowdsec cscli hub update
docker compose exec -T crowdsec cscli hub upgrade
docker compose exec -T crowdsec crowdsec -t
docker compose restart crowdsec