const assert = require('assert').strict;
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execFileSync, spawn} = require('child_process');

const root = process.cwd();
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'prodenv-web-security-'));
fs.chmodSync(directory, 0o755);
const prefix = `prodenv-web-security-${process.pid}`;
const containers = [];
const keep = process.env.WEB_SECURITY_PREVIEW === '1';
let success = false;
function run(executable, args, options = {}) {
  return execFileSync(executable, args, {encoding: 'utf8', timeout: 180000, ...options});
}
function yaml(filename) {
  return JSON.parse(run('docker', ['run', '--rm', '-i', '--entrypoint', 'yq',
    'crowdsecurity/crowdsec:v1.8.1', '-o=json', '.', '-'], {input: fs.readFileSync(filename, 'utf8')}));
}
function write(filename, value) {
  const destination = path.join(directory, filename);
  fs.mkdirSync(path.dirname(destination), {recursive: true});
  fs.writeFileSync(destination, typeof value === 'string' ? value : JSON.stringify(value));
  return destination;
}
function start(name, image, args, options = []) {
  const container = `${prefix}-${name}`;
  containers.push(container);
  run('docker', ['run', '-d', '--name', container, '--network', prefix, '--network-alias', name,
    '--label', `com.docker.compose.service=${name}`,
    '--memory', '512m', '--cpus', '1', '--pids-limit', '256',
    '-v', `${directory}:/fixtures:ro`, ...options, image, ...args]);
  return container;
}
function endpoint(container, port) {
  return 'http://' + run('docker', ['port', container, `${port}/tcp`]).trim();
}
function request(url, body, auth = false) {
  const args = ['-fsS', '--max-time', '20'];
  if (auth) args.push('-u', 'admin:fixture-security');
  if (body !== undefined) args.push('-H', 'Content-Type: application/json', '--data-binary', '@-');
  args.push(url);
  const response = run('curl', args, body === undefined ? {} : {input: JSON.stringify(body)});
  return response ? JSON.parse(response) : null;
}
function ready(url, auth = false) {
  run('curl', ['-fsS', '--retry', '30', '--retry-all-errors', '--retry-delay', '1',
    '--max-time', '2', '--retry-max-time', '60', ...(auth ? ['-u', 'admin:fixture-security'] : []), url],
    {stdio: ['ignore', 'pipe', 'pipe']});
}
function flatten(panels) {
  return panels.reduce((all, panel) => all.concat(panel, flatten(panel.panels || [])), []);
}

async function notificationRouteReady(grafanaUrl) {
  const deadline = Date.now() + 90000;
  const hasRoute = route => route && ((route.receiver === 'web-security-ntfy' && route.group_wait === '1s') ||
    (route.routes || []).some(hasRoute));
  while (Date.now() < deadline) {
    const config = request(grafanaUrl + '/api/alertmanager/grafana/config/api/v1/alerts', undefined, true);
    if (hasRoute(config.alertmanager_config.route)) return;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('Grafana did not activate the provisioned notification route');
}

function receiveNotification(url, token, status) {
  return new Promise((resolve, reject) => {
    const stream = spawn('curl', ['-fsSN', '--max-time', '120', '-H', `Authorization: Bearer ${token}`, url]);
    let pending = '';
    let received = false;
    stream.stdout.on('data', chunk => {
      pending += chunk.toString();
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines.filter(Boolean)) {
        const event = JSON.parse(line);
        if (event.event === 'message' && event.title.includes(status) && event.message.includes('WebSecurityRepeatedDetections')) {
          received = true;
          stream.kill();
          resolve(event);
        }
      }
    });
    stream.on('error', reject);
    stream.on('exit', () => { if (!received) reject(new Error(`No ${status} Grafana notification received within 120 seconds`)); });
  });
}

async function main() {
try {
  const pipeline = yaml('logging/promtail/config.yaml');
  const productionLoki = yaml('logging/loki/configs/config.yaml');
  const notifications = yaml('logging/grafana/provisioning/alerting/web-security.yml');
  const dashboard = JSON.parse(fs.readFileSync('logging/grafana/provisioning/dashboards/web-security.json'));
  const details = JSON.parse(fs.readFileSync('logging/grafana/provisioning/dashboards/web-requests.json'));
  const panels = flatten(dashboard.panels);
  assert.equal(dashboard.time.from, 'now-15m');
  assert.equal(dashboard.refresh, '');
  assert(!dashboard.panels.some(panel => panel.type === 'logs'));
  assert(dashboard.panels.find(panel => panel.id === 23).collapsed);
  assert(dashboard.panels.find(panel => panel.id === 24).collapsed);
  assert.equal(details.refresh, '');
  assert.equal(details.panels.find(panel => panel.type === 'logs').targets[0].maxLines, 100);
  const activity = panels.find(panel => panel.id === 9);
  const helpers = {};
  require('vm').runInNewContext(activity.options.helpers, {URLSearchParams, context: {
    grafana: {replaceVariables: value => value === '${__from}' ? '1000000' : '1900000'},
    handlebars: {registerHelper: (name, helper) => { helpers[name] = helper; }}
  }});
  const example = {event: 'crowdsec_alert', id: 'a'.repeat(64), scenario: 'crowdsecurity/http-probing',
    source_ip: '198.51.100.42', source_scope: 'Ip', sites: ['fixture.test'], events_count: 11,
    start_at: '2026-10-08T12:00:00Z', stop_at: '2026-10-08T12:00:10Z'};
  const grouped = helpers.detectionRows([{Line: JSON.stringify(example)}, {Line: JSON.stringify(example)}]);
  assert.equal(grouped.length, 1, 'Repeated exports must not duplicate the activity list');
  assert.equal(grouped[0].scenario, 'crowdsecurity/http-probing');
  assert.equal(helpers.detectionRows([{Line: JSON.stringify({...example, sites: ['_']})}])[0].siteLabel, 'Catch-all');
  const destination = new URL(grouped[0].href, 'http://localhost');
  assert.equal(destination.pathname, '/d/web-requests');
  assert.equal(destination.searchParams.get('refresh'), '');
  assert.equal(Number(destination.searchParams.get('from')), Date.parse(example.start_at) - 30000);
  assert.equal(Number(destination.searchParams.get('to')), Date.parse(example.stop_at) + 30000);
  assert.equal(destination.searchParams.get('var-alert'), example.id);
  assert(!JSON.stringify(dashboard).includes('probe='), 'Dashboard must not classify paths independently of CrowdSec');
  assert(!JSON.stringify(pipeline).includes('regexMatch'), 'Remove the custom path detector');
  for (const [index, first] of dashboard.panels.entries()) {
    for (const second of dashboard.panels.slice(index + 1)) {
      const firstBox = first.gridPos, secondBox = second.gridPos;
      assert(!(firstBox.x < secondBox.x + secondBox.w && firstBox.x + firstBox.w > secondBox.x && firstBox.y < secondBox.y + secondBox.h && firstBox.y + firstBox.h > secondBox.y));
    }
  }
  assert.equal(new Set(panels.map(panel => panel.id)).size, panels.length);
  for (const id of [7, 8]) {
    const panel = panels.find(panel => panel.id === id);
    assert.equal(panel.fieldConfig.defaults.displayName, '${__field.name}');
    assert.equal(panel.transformations[0].id, 'rowsToFields');
    assert.equal(panel.options.namePlacement, 'left');
  }
  for (const panel of panels.filter(panel => panel.type === 'logs')) {
    assert.equal(panel.options.showTime, false, 'Fixed timestamp columns squeeze log messages on phones');
    assert.equal(panel.options.wrapLogMessage, true);
    assert(panel.targets.every(target => /\{\{\.(time|stop_at)\}\}/.test(target.expr)));
  }
  assert(!notifications.policies, 'Must preserve existing notification policies');
  assert.equal(notifications.groups[0].rules[0].notification_settings.repeat_interval, '4h');
  assert.equal(notifications.contactPoints[0].receivers[0].settings.authorization_credentials, '$WEB_SECURITY_NTFY_TOKEN');
  const profile = yaml('logging/crowdsec/profiles.yaml');
  assert.equal(profile.filters[0], 'true');
  assert(!profile.decisions);
  assert.deepEqual(profile.notifications, ['loki_alerts']);
  const crowdsec = yaml('logging/crowdsec/docker-compose.yml').services.crowdsec;
  assert.equal(crowdsec.environment.DISABLE_ONLINE_API, 'true');
  assert(!crowdsec.ports);
  assert(!Object.keys(crowdsec.environment).some(key => /BOUNCER|ENROLL/.test(key)));
  assert.equal(productionLoki.limits_config.retention_stream[0].period, '7d');

  write('loki-production.json', productionLoki);
  run('docker', ['run', '--rm', '-v', `${directory}:/fixtures:ro`, 'grafana/loki:3.6.9',
    '-config.file=/fixtures/loki-production.json', '-verify-config=true']);

  const fixture = path.join(directory, 'compose');
  for (const filename of run('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z']).split('\0')) {
    if (!/\.ya?ml$/.test(filename) || !fs.existsSync(filename)) continue;
    const destination = path.join(fixture, filename);
    fs.mkdirSync(path.dirname(destination), {recursive: true});
    fs.copyFileSync(filename, destination);
    let parent = path.dirname(destination);
    while (parent.startsWith(fixture)) {
      fs.writeFileSync(path.join(parent, '.env'), '');
      parent = path.dirname(parent);
    }
  }
  const compose = JSON.parse(run('docker', ['compose', '--project-directory', fixture,
    '-f', path.join(fixture, 'docker-compose.yml'), '-f', path.join(fixture, 'docker-compose.ports.yml'),
    'config', '--no-env-resolution', '--format', 'json']));
  assert(!compose.services.crowdsec.ports);
  assert(Number(compose.services.crowdsec.mem_limit) > 0);
  assert(compose.services.nginx.healthcheck.test.join(' ').includes('127.0.0.1:8089'));
  const exposed = Object.entries(compose.services).flatMap(([name, service]) =>
    (service.ports || []).filter(port => !['127.0.0.1', '::1'].includes(port.host_ip)).map(port => `${name}:${port.published}`));
  console.log('Declared non-loopback bindings (not a reachability test): ' + exposed.join(', '));

  run('docker', ['network', 'create', prefix]);
  const gateway = JSON.parse(run('docker', ['network', 'inspect', prefix]))[0].IPAM.Config[0].Gateway;
  const nginxConfig = fs.readFileSync('nginx/nginx/nginx.conf', 'utf8');
  const logConfig = nginxConfig.slice(nginxConfig.indexOf('    map $request_uri'), nginxConfig.indexOf('    # Gzip'));
  const heartbeat = fs.readFileSync('nginx/nginx/conf.d/default.conf', 'utf8').split('# Catch-all HTTP')[0];
  write('nginx.conf', `events {}\nhttp {\n${logConfig}\n${heartbeat}
    set_real_ip_from ${gateway};
    real_ip_header X-Test-Client-IP;
    server { listen 8080 default_server; server_name _; return 444; }
    server {
      listen 8080; server_name fixture.test;
      location = /.env { return 200 'Synthetic test response'; }
      location = /.env.rare { return 200 'Rare synthetic response'; }
      location = /ok { return 200 'ok'; }
      location = /wp-login.php { proxy_pass http://127.0.0.1:8081; }
      location / { return 404; }
    }
    server { listen 8081; access_log off; return 401; }
    server { listen 8080; server_name second.test; return 404; }
  }`);
  const nginx = start('nginx', 'nginx:1.29.0', [], ['-p', '127.0.0.1::8080',
    '-v', `${directory}/nginx.conf:/etc/nginx/nginx.conf:ro`,
    '--health-cmd', 'curl -fsS http://127.0.0.1:8089/health', '--health-interval', '2s']);
  const nginxUrl = endpoint(nginx, 8080);
  const hit = (route, host = 'fixture.test', source = '198.51.100.42') => {
    try {
      return run('curl', ['-sS', '--path-as-is', '-o', '/dev/null', '-w', '%{http_code}',
        '-H', `Host: ${host}`, '-H', `X-Test-Client-IP: ${source}`,
        '-H', 'Authorization: Bearer SECRET', nginxUrl + route]);
    } catch (error) {
      if (error.status === 52 && host === 'unknown.test') return '444';
      throw error;
    }
  };
  hit('/ok');
  const acquisition = yaml('logging/crowdsec/acquis.yaml');
  acquisition.container_name = [nginx];
  write('acquis.yaml', acquisition);
  fs.mkdirSync(path.join(directory, 'crowdsec-config'));
  fs.mkdirSync(path.join(directory, 'crowdsec-data'));
  const detector = start('crowdsec', crowdsec.image, [], [
    ...Object.entries(crowdsec.environment).flatMap(([key, value]) => ['-e', `${key}=${value}`]),
    '-p', '127.0.0.1::6060', '-v', '/var/run/docker.sock:/var/run/docker.sock:ro',
    '-v', `${directory}/crowdsec-config:/etc/crowdsec`, '-v', `${directory}/crowdsec-data:/var/lib/crowdsec/data`,
    '-v', `${directory}/acquis.yaml:/etc/crowdsec/acquis.yaml:ro`,
    ...['config.yaml.local', 'profiles.yaml'].flatMap(filename => ['-v', `${root}/logging/crowdsec/${filename}:/etc/crowdsec/${filename}:ro`]),
    '-v', `${root}/logging/crowdsec/loki-notification.yaml:/etc/crowdsec/notifications/loki.yaml:ro`,
    '-v', `${root}/logging/crowdsec/nginx-json.yaml:/etc/crowdsec/parsers/s01-parse/prodenv-nginx-json.yaml:ro`]);
  ready(endpoint(detector, 6060) + '/metrics');
  const loki = {...productionLoki, auth_enabled: false, analytics: {reporting_enabled: false}};
  loki.common = {path_prefix: '/loki', replication_factor: 1, instance_addr: '127.0.0.1',
    storage: {filesystem: {chunks_directory: '/loki/chunks', rules_directory: '/loki/rules'}},
    ring: {kvstore: {store: 'inmemory'}}};
  loki.schema_config = {configs: [{from: '2024-01-01', store: 'tsdb', object_store: 'filesystem', schema: 'v13', index: {prefix: 'index_', period: '24h'}}]};
  loki.ruler = {storage: {type: 'local', local: {directory: '/loki/rules'}}};
  loki.compactor.delete_request_store = 'filesystem';
  loki.ingester = {...loki.ingester, lifecycler: {min_ready_duration: '0s'}};
  write('loki.json', loki);
  const lokiContainer = start('loki', 'grafana/loki:3.6.9', ['-config.file=/fixtures/loki.json'],
    ['-p', '127.0.0.1::3100', '--tmpfs', '/loki:mode=1777']);
  const lokiUrl = endpoint(lokiContainer, 3100);
  ready(lokiUrl + '/ready');

  pipeline.positions.filename = '/tmp/positions.yaml';
  pipeline.clients = [{url: 'http://loki:3100/loki/api/v1/push', batchwait: '10ms'}];
  pipeline.scrape_configs[0].docker_sd_configs[0].filters = [{name: 'name', values: [nginx, detector]}];
  pipeline.scrape_configs[0].relabel_configs.unshift({source_labels: ['__meta_docker_container_name'], regex: `/${prefix}-(nginx|crowdsec)`, action: 'keep'});
  pipeline.scrape_configs[0].relabel_configs[1].regex = `/${prefix}-(.*)`;
  write('promtail.json', pipeline);
  const promtail = start('promtail', 'grafana/promtail:2.7.1', ['-config.file=/fixtures/promtail.json'],
    ['-p', '127.0.0.1::9080', '-v', '/var/run/docker.sock:/var/run/docker.sock:ro']);
  const promtailUrl = endpoint(promtail, 9080);
  ready(promtailUrl + '/ready');
  hit('/.env?token=SECRET');
  hit('/wp-login.php');
  hit('/folder/%2e%2e/probe');
  hit('/.env', 'unknown.test');
  hit('/.env.rare', 'fixture.test', '198.51.100.77');
  for (let index = 0; index < 30; index++) hit(`/wp-admin/unique-${index}`);
  run('docker', ['exec', nginx, 'curl', '--parallel', '--parallel-max', '20', '-s', '-o', '/dev/null',
    ...Array.from({length: 200}, () => 'http://127.0.0.1:8080/wp-admin/repeated')
      .flatMap(url => ['-H', 'Host: fixture.test', '-o', '/dev/null', url])], {stdio: 'pipe'});
  for (let index = 0; index < 20; index++) hit(`/nonexistent-${index}`);
  for (let index = 0; index < 20; index++) hit(`/random-scan-${index}`, index % 2 ? 'fixture.test' : 'second.test', '198.51.100.88');
  const securityRules = yaml('logging/prometheus/web-security-alerts.yml');
  for (const group of securityRules.groups) for (const rule of group.rules) {
    rule.for = '1s';
    if (rule.alert === 'WebSecurityRepeatedDetections') rule.expr = rule.expr.replace('[10m]', '[30s]').replace('> 20', '> 0');
  }
  write('security-rules.yaml', securityRules);
  write('prometheus.json', {global: {scrape_interval: '1s', evaluation_interval: '1s'},
    rule_files: ['/fixtures/security-rules.yaml'], scrape_configs: [
    {job_name: 'crowdsec', static_configs: [{targets: ['crowdsec:6060']}]},
    {job_name: 'promtail', static_configs: [{targets: ['promtail:9080']}]},
    {job_name: 'loki', static_configs: [{targets: ['loki:3100']}]}]});
  const prometheus = start('prometheus', 'prom/prometheus:v2.47.1', ['--config.file=/fixtures/prometheus.json'], ['-p', '127.0.0.1::9090']);
  const prometheusUrl = endpoint(prometheus, 9090);
  ready(prometheusUrl + '/-/ready');
  const ntfy = start('ntfy', 'binwiederhier/ntfy:v2.26', ['serve'], ['-p', '127.0.0.1::80',
    '--tmpfs', '/var/lib/ntfy:mode=1777',
    '-e', 'NTFY_AUTH_FILE=/var/lib/ntfy/auth.db', '-e', 'NTFY_AUTH_DEFAULT_ACCESS=deny-all',
    '-e', 'NTFY_CACHE_FILE=/var/lib/ntfy/cache.db']);
  const ntfyUrl = endpoint(ntfy, 80);
  ready(ntfyUrl + '/v1/health');
  run('docker', ['exec', '-e', 'NTFY_PASSWORD=fixture-notify', ntfy, 'ntfy', 'user', 'add', '--role=admin', 'fixture'], {stdio: 'pipe'});
  const token = run('docker', ['exec', ntfy, 'ntfy', 'token', 'add', 'fixture'], {stdio: 'pipe'}).match(/tk_[A-Za-z0-9]+/)[0];
  write('provisioning/datasources/security.yaml', {apiVersion: 1, datasources: [
    {name: 'Prometheus', uid: 'Prometheus', type: 'prometheus', access: 'proxy', url: 'http://prometheus:9090'},
    {name: 'Loki', uid: 'P8E80F9AEF21F6940', type: 'loki', access: 'proxy', url: 'http://loki:3100'}]});
  notifications.contactPoints[0].receivers[0].settings.authorization_credentials = token;
  notifications.groups[0].interval = '10s';
  notifications.groups[0].rules[0].notification_settings.group_wait = '1s';
  notifications.groups[0].rules[0].notification_settings.group_interval = '5s';
  write('provisioning/alerting/security.yaml', notifications);
  write('provisioning/dashboards/default.yaml', {apiVersion: 1, providers: [{name: 'Security', type: 'file', options: {path: '/dashboards'}}]});
  const grafana = start('grafana', 'grafana/grafana:11.6.0', [], ['-p', '127.0.0.1::3000',
    '-e', 'GF_SECURITY_ADMIN_PASSWORD=fixture-security', '-e', 'GF_AUTH_ANONYMOUS_ENABLED=false',
    '-e', 'GF_ANALYTICS_REPORTING_ENABLED=false',
    '-e', 'GF_INSTALL_PLUGINS=marcusolsson-dynamictext-panel 6.2.0',
    '-e', 'GF_PLUGINS_PREINSTALL_DISABLED=true',
    '-v', `${directory}/provisioning:/etc/grafana/provisioning:ro`,
    '-v', `${root}/logging/grafana/provisioning/dashboards:/dashboards:ro`]);
  const grafanaUrl = endpoint(grafana, 3000);
  ready(grafanaUrl + '/api/health');
  ready(grafanaUrl + '/api/dashboards/uid/web-security', true);
  const metrics = run('curl', ['-fsS', promtailUrl + '/metrics']).split('\n')
    .filter(line => line.startsWith('nginx_security_')).join('\n');
  assert(metrics.includes('nginx_security_requests_total'));
  assert(metrics.includes('outcome="upstream"'));
  assert(metrics.includes('outcome="edge_rejected"'));
  assert(!metrics.includes('probe='));
  assert(!metrics.includes('198.51.100.42'));
  assert(!metrics.includes('path="'));
  console.log('Real Promtail classification and bounded metric labels passed.');
  assert.equal(request(grafanaUrl + '/api/dashboards/uid/web-security', undefined, true).dashboard.title, 'Web Security');
  assert.equal(request(grafanaUrl + '/api/dashboards/uid/web-requests', undefined, true).dashboard.refresh, '');
  const provisioned = request(grafanaUrl + '/api/v1/provisioning/alert-rules', undefined, true);
  assert(provisioned.some(rule => rule.uid === 'web-security-notifications' && rule.notification_settings.receiver === 'web-security-ntfy'));
  assert.equal(run('curl', ['-s', '-o', '/dev/null', '-w', '%{http_code}', grafanaUrl + '/api/dashboards/uid/web-security']), '401');
  console.log('Waiting for Grafana to activate the provisioned notification route.');
  await notificationRouteReady(grafanaUrl);
  let scanNumber = 100;
  const triggerDetection = () => {
    const source = `198.51.100.${scanNumber++}`;
    for (let index = 0; index < 12; index++) hit(`/random-${index}`, 'fixture.test', source);
  };
  triggerDetection();
  let requestError;
  const traffic = setInterval(() => {
    try { triggerDetection(); } catch (error) { requestError = error; }
  }, 5000);
  let notice;
  try {
    notice = await receiveNotification(ntfyUrl + '/security/json?since=all', token, 'firing');
  } finally {
    clearInterval(traffic);
  }
  if (requestError) throw requestError;
  assert(notice.message.includes('crowdsecurity/http-probing'));
  assert(!notice.message.includes('"alerts"'));
  const detected = JSON.parse(run('docker', ['exec', detector, 'cscli', 'alerts', 'list', '-o', 'json']));
  assert(detected.some(alert => alert.scenario === 'crowdsecurity/http-probing'));
  assert.deepEqual(JSON.parse(run('docker', ['exec', detector, 'cscli', 'decisions', 'list', '-o', 'json'])), []);
  const exported = request(lokiUrl + '/loki/api/v1/query_range?query=' + encodeURIComponent('{security="crowdsec"}') + '&limit=100');
  const records = exported.data.result.flatMap(stream => stream.values.map(value => JSON.parse(value[1])));
  assert(records.some(record => record.scenario === 'crowdsecurity/http-probing'));
  assert(records.every(record => /^[a-f0-9]{64}$/.test(record.id) && record.event === 'crowdsec_alert' && record.source_ip));
  assert(records.some(record => record.sites.includes('fixture.test')));
  assert(records.every(record => !record.alert.decisions || record.alert.decisions.length === 0));
  assert(!records.some(record => record.source_ip === '198.51.100.77'), 'A single sensitive-looking path must not be promoted by custom rules');
  const multiSite = records.find(record => record.source_ip === '198.51.100.88');
  assert.equal(multiSite.site, 'Multiple sites');
  assert.deepEqual(multiSite.sites, ['fixture.test', 'second.test']);
  assert(multiSite.alert.events.some(event => event.meta.some(item => item.key === 'http_path' && item.value.startsWith('/random-scan-'))));
  const unknownTime = new Date().toISOString();
  run('docker', ['exec', detector, 'cscli', 'notifications', 'test', 'loki_alerts', '--alert', JSON.stringify({
    scenario: 'fixture/missing-site-evidence', source: {scope: 'Ip', value: '198.51.100.250', ip: '198.51.100.250'},
    start_at: unknownTime, stop_at: unknownTime, events_count: 1, decisions: [],
    events: [{timestamp: unknownTime, meta: [{key: 'target_fqdn', value: 'untrusted.example'}]}]
  })], {stdio: 'pipe'});
  const missingSite = request(lokiUrl + '/loki/api/v1/query_range?query=' + encodeURIComponent('{security="crowdsec"} | json scenario | scenario="fixture/missing-site-evidence"'));
  const unknownRecord = JSON.parse(missingSite.data.result[0].values[0][1]);
  assert.equal(unknownRecord.site, 'Unknown');
  assert.deepEqual(unknownRecord.sites, ['Unknown']);
  assert(!exported.data.result.some(stream => Object.keys(stream.stream).some(key => ['source_ip', 'id', 'sites'].includes(key))), 'Alert identifiers and IPs must not become indexed Loki labels');
  console.log('Native CrowdSec structured alerts and site evidence reached Loki without ban decisions.');
  assert.equal(hit('/ok'), '200');
  const access = run('docker', ['logs', nginx], {stdio: ['ignore', 'pipe', 'pipe']});
  assert(!access.includes('SECRET'));
  console.log('Real HTTP -> Docker discovery -> CrowdSec detection -> Grafana -> authenticated ntfy delivery passed; no bans.');

  for (const panel of panels) {
    for (const target of panel.targets || []) {
      for (const [site, source] of [['.*', ''], ['fixture\\.test', '198\\.51\\.100\\.'], ['fixture\\.test', '203\\.0\\.113\\.']]) {
        const expression = target.expr.replace(/\$\{site:regex\}/g, site).replace(/\$\{source:regex\}/g, source)
          .replace(/\$__range/g, '1h').replace(/\$__interval/g, '5m');
        const isLoki = panel.datasource.type === 'loki';
        const base = isLoki ? lokiUrl + '/loki/api/v1/' : prometheusUrl + '/api/v1/';
        const route = isLoki && (panel.type === 'logs' || panel.id === 9) ? 'query_range' : 'query';
        const response = request(base + route + '?query=' + encodeURIComponent(expression));
        assert.equal(response.status, 'success', panel.title);
        if (panel.id === 9) {
          const rows = response.data.result.flatMap(stream => stream.values.map(value => ({Line: value[1]})));
          const detections = helpers.detectionRows(rows);
          assert(detections.length <= 25);
          if (source === '') assert(detections.some(record => record.scenario === 'crowdsecurity/http-probing'));
        }
        if (source === '' && [2, 3, 4, 7, 8, 9, 10, 11].includes(panel.id)) assert(response.data.result.length > 0, panel.title + ' must contain fixture data');
        if (panel.id === 2 && source.startsWith('198')) assert(response.data.result.length > 0, 'Matching IP prefix must return requests');
        if (panel.id === 2 && source.startsWith('203')) assert.equal(response.data.result.length, 0, 'Nonmatching IP prefix must return no events');
      }
    }
  }
  const selected = records.find(record => record.source_ip === '198.51.100.88');
  for (const panel of details.panels) {
    const expression = panel.targets[0].expr.replace(/\$\{(alert|source):doublequote\}/g,
      (_, name) => JSON.stringify(name === 'alert' ? selected.id : selected.source_ip));
    const response = request(lokiUrl + '/loki/api/v1/query_range?limit=100&direction=backward&query=' + encodeURIComponent(expression));
    assert.equal(response.status, 'success', panel.title);
    assert(response.data.result.length > 0, panel.title);
    if (panel.id === 1) assert.equal(JSON.parse(response.data.result[0].values[0][1]).id, selected.id);
    if (panel.type === 'logs') assert(response.data.result.reduce((total, stream) => total + stream.values.length, 0) <= 100);
  }
  const resolved = await receiveNotification(ntfyUrl + '/security/json?since=all', token, 'resolved');
  assert(resolved.message.includes('crowdsecurity/http-probing'));
  console.log('Firing and resolved notifications both reached authenticated ntfy.');
  console.log('All dashboard queries, Grafana rule provisioning and ntfy formatting passed.');
  if (keep) {
    console.log(`Preview: ${grafanaUrl}/d/web-security`);
    console.log(`Local-only synthetic preview containers: ${containers.join(' ')}; network: ${prefix}; fixtures: ${directory}`);
  }
  success = true;
} catch (error) {
  for (const container of containers) {
    try { run('docker', ['logs', '--tail', '12', container], {stdio: 'inherit'}); } catch (_) {}
  }
  throw error;
} finally {
  if (!keep || !success) {
    for (const container of containers.reverse()) {
      try { run('docker', ['rm', '-f', container], {stdio: 'ignore'}); } catch (_) {}
    }
    try { run('docker', ['network', 'rm', prefix], {stdio: 'ignore'}); } catch (_) {}
    run('docker', ['run', '--rm', '-v', `${directory}:/cleanup`, '--entrypoint', 'sh',
      'crowdsecurity/crowdsec:v1.8.1', '-c', 'rm -rf /cleanup/*'], {stdio: 'ignore'});
    fs.rmdirSync(directory, {recursive: true});
  }
}
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });