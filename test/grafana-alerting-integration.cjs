const assert = require('assert').strict;
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execFileSync} = require('child_process');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'prodenv-grafana-alerting-'));
fs.chmodSync(directory, 0o755);
const network = `prodenv-grafana-alerting-${process.pid}`;
const containers = [];
let metricsTimer;
const counts = {'#chat': {total: 100, bot: 3}, '#quiet': {total: 100, bot: 3}, '#short': {total: 99, bot: 4}};

function run(command, args, options = {}) {
  return execFileSync(command, args, {encoding: 'utf8', timeout: 120000, ...options});
}
function write(filename, data) {
  const destination = path.join(directory, filename);
  fs.mkdirSync(path.dirname(destination), {recursive: true});
  fs.writeFileSync(destination, typeof data === 'string' ? data : JSON.stringify(data));
  return destination;
}
function start(name, image, args, options = []) {
  const container = `${network}-${name}`;
  containers.push(container);
  run('docker', ['run', '-d', '--name', container, '--network', network, '--network-alias', name,
    '--memory', '512m', '--pids-limit', '256', '-v', `${directory}:/fixtures:ro`, ...options, image, ...args]);
  return container;
}
function endpoint(container, port) {
  return 'http://' + run('docker', ['port', container, `${port}/tcp`]).trim();
}
function request(url, auth) {
  return JSON.parse(run('curl', ['-fsS', '--max-time', '5', ...(auth ? ['-H', auth] : []), url],
    {stdio: ['ignore', 'pipe', 'pipe']}));
}
async function waitFor(label, predicate, timeout = 90000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try { const result = predicate(); if (result) return result; } catch (error) { lastError = error; }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error(`${label} timed out${lastError ? ': ' + lastError.message : ''}`);
}
function publishMetrics() {
  const lines = [`bot_metrics_snapshot_timestamp_seconds{platform="sinair"} ${Date.now() / 1000}`];
  for (const [room, values] of Object.entries(counts)) {
    const labels = `platform="sinair",room="${room}",window="100"`;
    lines.push(`bot_chat_window_messages{${labels}} ${values.total}`, `bot_chat_window_bot_messages{${labels}} ${values.bot}`);
  }
  write('exporter/metrics.next', lines.join('\n') + '\n');
  fs.renameSync(path.join(directory, 'exporter/metrics.next'), path.join(directory, 'exporter/metrics'));
}

async function main() {
  try {
    run('docker', ['network', 'create', network]);
    publishMetrics();
    metricsTimer = setInterval(publishMetrics, 1000);
    start('metrics', 'busybox:latest', ['httpd', '-f', '-p', '8080', '-h', '/fixtures/exporter']);
    write('prometheus.json', {
      global: {scrape_interval: '1s', evaluation_interval: '1s'},
      rule_files: ['/rules/bot-alerts.yml'],
      scrape_configs: [{job_name: 'sinair-llm-bot-prod', static_configs: [{targets: ['metrics:8080']}]}],
    });
    const prometheus = start('prometheus', 'prom/prometheus:v2.47.1', ['--config.file=/fixtures/prometheus.json'],
      ['-p', '127.0.0.1::9090', '-v', `${root}/logging/prometheus:/rules:ro`]);
    const prometheusUrl = endpoint(prometheus, 9090);
    await waitFor('Prometheus', () => request(prometheusUrl + '/api/v1/status/buildinfo').status === 'success');

    const ntfy = start('ntfy', 'binwiederhier/ntfy:v2.26', ['serve'], ['-p', '127.0.0.1::80',
      '--tmpfs', '/var/lib/ntfy:mode=1777', '-e', 'NTFY_AUTH_FILE=/var/lib/ntfy/auth.db',
      '-e', 'NTFY_AUTH_DEFAULT_ACCESS=deny-all', '-e', 'NTFY_CACHE_FILE=/var/lib/ntfy/cache.db']);
    const ntfyUrl = endpoint(ntfy, 80);
    await waitFor('ntfy', () => request(ntfyUrl + '/v1/health').healthy);
    run('docker', ['exec', '-e', 'NTFY_PASSWORD=fixture-alerting', ntfy, 'ntfy', 'user', 'add', 'fixture'], {stdio: 'pipe'});
    for (const topic of ['alert', 'security']) run('docker', ['exec', ntfy, 'ntfy', 'access', 'fixture', topic, 'rw'], {stdio: 'pipe'});
    const token = run('docker', ['exec', ntfy, 'ntfy', 'token', 'add', 'fixture'], {stdio: 'pipe'}).match(/tk_[A-Za-z0-9]+/)[0];
    assert(['401', '403'].includes(run('curl', ['-sS', '-o', '/dev/null', '-w', '%{http_code}',
      '-d', 'unauthenticated fixture', ntfyUrl + '/alert'])));

    fs.cpSync(path.join(root, 'logging/grafana/provisioning'), path.join(directory, 'provisioning'), {recursive: true});
    const grafana = start('grafana', 'grafana/grafana:11.6.0', [], ['-p', '127.0.0.1::3000',
      '-e', 'GF_SECURITY_ADMIN_PASSWORD=fixture-alerting', '-e', 'GF_ANALYTICS_REPORTING_ENABLED=false',
      '-e', 'GF_ANALYTICS_CHECK_FOR_UPDATES=false', '-e', 'GF_PLUGINS_PREINSTALL_DISABLED=true',
      '-e', `NTFY_TOKEN=${token}`, '-e', `WEB_SECURITY_NTFY_TOKEN=${token}`,
      '-v', `${directory}/provisioning:/etc/grafana/provisioning:ro`]);
    const grafanaUrl = endpoint(grafana, 3000);
    const grafanaAuth = 'Authorization: Basic ' + Buffer.from('admin:fixture-alerting').toString('base64');
    await waitFor('Grafana startup with all production provisioning files', () => request(grafanaUrl + '/api/health').database === 'ok');
    const rules = await waitFor('All provisioned alert rules', () => {
      const result = request(grafanaUrl + '/api/v1/provisioning/alert-rules', grafanaAuth);
      return ['sinair-llm-bot-prod-chat-participation', 'web-security-notifications']
        .every(uid => result.some(rule => rule.uid === uid)) && result;
    });
    for (const filename of fs.readdirSync(path.join(root, 'logging/grafana/provisioning/dashboards')).filter(name => name.endsWith('.json'))) {
      const source = JSON.parse(fs.readFileSync(path.join(root, 'logging/grafana/provisioning/dashboards', filename)));
      await waitFor(`Provisioned dashboard ${source.uid}`, () =>
        request(grafanaUrl + '/api/dashboards/uid/' + source.uid, grafanaAuth).dashboard.title === source.title);
    }
    console.log('All production Grafana alert files and dashboards provisioned successfully.');

    const liveRules = () => request(grafanaUrl + '/api/prometheus/grafana/api/v1/rules', grafanaAuth).data.groups.flatMap(group => group.rules);
    const botRule = () => liveRules().find(rule => rule.name === 'sinair-llm-bot-prod chat participation');
    await waitFor('Healthy initial rule evaluation', () => {
      const rule = botRule();
      return rule && rule.health === 'ok' && rule.lastEvaluation && !rule.lastEvaluation.startsWith('0001') && rule.state === 'inactive';
    });
    const notices = () => run('curl', ['-fsS', '--max-time', '5', '-H', `Authorization: Bearer ${token}`,
      ntfyUrl + '/alert/json?poll=1&since=all']).split('\n').filter(Boolean).map(JSON.parse).filter(event => event.event === 'message');
    assert.equal(notices().length, 0, 'Three bot messages and incomplete windows must not notify');

    counts['#chat'].bot = 4;
    publishMetrics();
    const first = await waitFor('Four-message alert delivered through Prometheus, Grafana and authenticated ntfy', () => notices()[0]);
    assert(first.title.includes('sinair-llm-bot-prod'));
    assert(first.message.includes('sent 4 of the last 100 messages in #chat'), first.message);
    assert(!first.message.includes('#quiet') && !first.message.includes('#short'));
    assert(!first.message.includes('"alerts"'), 'Notification must be formatted text');
    console.log('Four messages trigger only the matching room and reach the authenticated alert topic.');

    counts['#chat'].bot = 5;
    publishMetrics();
    await new Promise(resolve => setTimeout(resolve, 65000));
    assert.equal(notices().length, 1, 'The fifth bot message must not create a second notification');
    assert.equal(botRule().health, 'ok');
    console.log('The fifth bot message keeps the alert active without a second notification.');

    counts['#chat'].bot = 3;
    publishMetrics();
    await waitFor('Alert recovery', () => botRule().state === 'inactive');
    await new Promise(resolve => setTimeout(resolve, 65000));
    assert.equal(notices().length, 1, 'Recovery notifications are disabled');
    counts['#quiet'].bot = 4;
    publishMetrics();
    await waitFor('Independent second-room notification', () => notices().find(event => event.message.includes('in #quiet')));
    assert.equal(notices().length, 2);
    console.log('Fifth-message deduplication, recovery suppression and independent room notifications passed.');

    run('docker', ['restart', grafana]);
    const restartedUrl = endpoint(grafana, 3000);
    await waitFor('Grafana restart', () => request(restartedUrl + '/api/health').database === 'ok');
    assert.equal(request(restartedUrl + '/api/v1/provisioning/alert-rules', grafanaAuth).length, rules.length);
    console.log('Grafana restart with existing state and unchanged provisioning passed.');
  } catch (error) {
    for (const container of containers) {
      try { run('docker', ['logs', '--tail', '15', container], {stdio: 'inherit'}); } catch (_) {}
    }
    throw error;
  } finally {
    clearInterval(metricsTimer);
    for (const container of containers.reverse()) {
      try { run('docker', ['rm', '-f', '-v', container], {stdio: 'ignore'}); } catch (_) {}
    }
    try { run('docker', ['network', 'rm', network], {stdio: 'ignore'}); } catch (_) {}
    fs.rmSync(directory, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
