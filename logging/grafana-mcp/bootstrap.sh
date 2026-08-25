#!/bin/sh
set -eu

account_name=sinair-llm-bot-mcp
token_name=grafana-mcp
token_file=/output/grafana-token
grafana_url=${GRAFANA_URL:-http://grafana:3000}

log() {
    printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

on_error() {
    status=$?
    log "FAILED (exit code ${status}) at line ${1}"
}
trap 'on_error $LINENO' ERR

request() {
    curl --fail --silent --show-error \
    --user "${GF_SECURITY_ADMIN_USER}:${GF_SECURITY_ADMIN_PASSWORD}" \
    --header 'Content-Type: application/json' \
    "$@"
}

log "waiting for grafana to become healthy at ${grafana_url}"
until request "${grafana_url}/api/health" >/dev/null 2>&1; do
    sleep 2
done
log "grafana is healthy"

log "looking up service account '${account_name}'"
account_id=$(request "${grafana_url}/api/serviceaccounts/search?query=${account_name}" \
    | jq -r --arg name "$account_name" \
    '.serviceAccounts[] | select(.name == $name) | .id' \
    | head -n 1)

if [ -z "$account_id" ]; then
    log "service account not found, creating it"
    account_id=$(request \
    --request POST \
    --data "$(jq -cn --arg name "$account_name" \
        '{name: $name, role: "Viewer", isDisabled: false}')" \
    "${grafana_url}/api/serviceaccounts" \
    | jq -r '.id')
    log "created service account id=${account_id}"
else
    log "found existing service account id=${account_id}, ensuring role/enabled state"
    request \
    --request PATCH \
    --data '{"role":"Viewer","isDisabled":false}' \
    "${grafana_url}/api/serviceaccounts/${account_id}" >/dev/null
fi

if [ -s "$token_file" ] && curl --fail --silent \
    --header "Authorization: Bearer $(cat "$token_file")" \
    "${grafana_url}/api/org" >/dev/null; then
    log "existing token is still valid, nothing to do"
    exit 0
fi
log "no valid existing token found, issuing a new one"

log "removing any stale tokens named '${token_name}'"
request "${grafana_url}/api/serviceaccounts/${account_id}/tokens" \
    | jq -r --arg name "$token_name" '.[] | select(.name == $name) | .id' \
    | while read -r token_id; do
        log "deleting stale token id=${token_id}"
        request --request DELETE \
        "${grafana_url}/api/serviceaccounts/${account_id}/tokens/${token_id}" \
        >/dev/null
    done

log "creating new token '${token_name}'"
token=$(request \
    --request POST \
    --data "$(jq -cn --arg name "$token_name" \
    '{name: $name, secondsToLive: 0}')" \
    "${grafana_url}/api/serviceaccounts/${account_id}/tokens" \
    | jq -er '.key')

log "writing token to ${token_file}"
umask 077
printf '%s' "$token" > "${token_file}.tmp"
chown 1000:1000 "${token_file}.tmp"
chmod 0600 "${token_file}.tmp"
mv "${token_file}.tmp" "$token_file"
log "bootstrap complete"