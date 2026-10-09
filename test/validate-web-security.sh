#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
sh -n logging/crowdsec/update-rules.sh
temporary="$(mktemp -d)"
container="prodenv-security-test-$$"
trap 'docker rm -f "$container" "$container-replay" >/dev/null 2>&1 || true; docker volume rm "$container-config" "$container-data" >/dev/null 2>&1 || true; rm -rf "$temporary"' EXIT
chmod 755 "$temporary"
export SECURITY_TEST_DIRECTORY="$temporary"
node <<'NODE'
const assert = require('assert').strict;
const fs = require('fs');
const config = fs.readFileSync('nginx/nginx/nginx.conf', 'utf8');
const logging = config.slice(config.indexOf('    map $request_uri'), config.indexOf('    # Gzip'));
assert(logging.includes('escape=json'));
for (const filename of ['default.conf', 'include/assets.conf']) {
  assert(!/access_log\s+off/.test(fs.readFileSync(`nginx/nginx/conf.d/${filename}`, 'utf8')));
}
assert(!/\$(request|args|http_authorization|http_cookie|http_referer|http_x_forwarded_for)\b/.test(logging));
fs.writeFileSync(`${process.env.SECURITY_TEST_DIRECTORY}/nginx.conf`, `
events {}
http {
${logging.replace('buffer=32k flush=5s', '')}
server { listen 8080 default_server; server_name _; return 444; }
server {
  listen 8080; server_name fixture.test;
  location /ok { return 200 'ok'; }
  location /deny { return 403; }
  location /upstream { proxy_pass http://127.0.0.1:8081; }
  location / { return 404; }
}
server { listen 8081; access_log off; return 418; }
}
`);
NODE
docker run -d --name "$container" -p 127.0.0.1::8080 \
  -v "$temporary/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:1.29.0 >/dev/null
docker exec "$container" nginx -t
port="$(docker port "$container" 8080/tcp | cut -d: -f2)"
for path in '/ok?token=SECRET' '/deny' '/.env' '/upstream' '/missing.js' '/quote%22path'; do
  curl --retry 3 --retry-connrefused -sS -o /dev/null -H 'Host: fixture.test' \
    -H 'X-Forwarded-For: 198.51.100.10' -H 'Authorization: Bearer SECRET' \
    "http://127.0.0.1:$port$path"
done
curl -sS -o /dev/null -H 'Host: unknown.test' "http://127.0.0.1:$port/probe" || test "$?" = 52
docker logs "$container" > "$temporary/access.log" 2>/dev/null
node <<'NODE'
const assert = require('assert').strict;
const fs = require('fs');
const raw = fs.readFileSync(`${process.env.SECURITY_TEST_DIRECTORY}/access.log`, 'utf8');
const events = raw.split('\n').filter(line => line.startsWith('{')).map(JSON.parse);
assert.equal(events.length, 7);
assert(!raw.includes('SECRET'));
assert(events.every(event => event.source_ip !== '198.51.100.10' && !event.path.includes('?')));
assert(events.some(event => event.host === 'unknown.test' && event.site === '_' && event.status === 444));
assert(events.some(event => event.path === '/upstream' && event.upstream_status === '418' && event.upstream !== ''));
assert(events.some(event => event.path === '/.env' && event.status === 404 && event.upstream === ''));
assert(events.some(event => event.path === '/missing.js'));
console.log('Nginx security logs: attribution, outcomes, escaping and query/header exclusion passed.');
NODE

crowdsec_mounts=(
  -v "$container-config:/etc/crowdsec"
  -v "$container-data:/var/lib/crowdsec/data"
  -v "$PWD/logging/crowdsec/config.yaml.local:/etc/crowdsec/config.yaml.local:ro"
  -v "$PWD/logging/crowdsec/profiles.yaml:/etc/crowdsec/profiles.yaml:ro"
  -v "$PWD/logging/crowdsec/loki-notification.yaml:/etc/crowdsec/notifications/loki.yaml:ro"
  -v "$PWD/logging/crowdsec/nginx-json.yaml:/etc/crowdsec/parsers/s01-parse/prodenv-nginx-json.yaml:ro"
  -v "$temporary:/fixtures"
)
docker run --rm "${crowdsec_mounts[@]}" -e COLLECTIONS=crowdsecurity/nginx \
  -e DISABLE_ONLINE_API=true -e TEST_MODE=true crowdsecurity/crowdsec:v1.8.1 > "$temporary/crowdsec-config.log" 2>&1 || {
    tail -40 "$temporary/crowdsec-config.log"
    exit 1
  }
node <<'NODE'
const fs = require('fs');
const now = Date.now();
const events = Array.from({length: 20}, (_, index) => JSON.stringify({
  event: 'nginx_access', time: new Date(now + index * 1000).toISOString(),
  source_ip: '198.51.100.42', host: 'fixture.test', site: 'fixture.test',
  path: `/nonexistent-${index}`, method: 'GET', status: 404,
  upstream: '', upstream_status: '', bytes: 0, duration: 0.001
}));
fs.writeFileSync(`${process.env.SECURITY_TEST_DIRECTORY}/probes.log`, events.join('\n') + '\n');
NODE
timeout --signal=TERM 15s docker run --name "$container-replay" "${crowdsec_mounts[@]}" --entrypoint crowdsec crowdsecurity/crowdsec:v1.8.1 \
  -dsn file:///fixtures/probes.log -type prodenv-nginx > "$temporary/detection.log" 2>&1 || {
    result="$?"
    if [ "$result" != 124 ]; then
      tail -40 "$temporary/detection.log"
      exit 1
    fi
  }
grep -q 'Ip 198.51.100.42 performed.*crowdsecurity/http-probing' "$temporary/detection.log" || {
  tail -40 "$temporary/detection.log"
  exit 1
}
echo 'CrowdSec parser and maintained HTTP probing scenario passed.'
docker run --rm --entrypoint promtool -v "$PWD:/work:ro" -w /work \
  prom/prometheus:v2.47.1 test rules test/web-security-rules.test.yml
node test/web-security-integration.cjs