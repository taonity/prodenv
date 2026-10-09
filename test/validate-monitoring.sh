#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
temporary="$(mktemp -d)"
chmod 755 "$temporary"
trap 'rm -rf "$temporary"' EXIT
export METRICS_DIRECTORY="$temporary/metrics"
export PYTHONDONTWRITEBYTECODE=1

sh -n backup/scripts/record-backup-metric.sh
sh -n backup/scripts/verify-postgres-restore.sh
sh -n backup/scripts/validate-postgres-restore.sh
sh -n logging/host-health/install.sh
sh -n logging/host-health/run.sh
python3 -m unittest discover -s logging/host-health -p 'test_*.py'
python3 - <<'PY' | docker run --rm -i --entrypoint promtool prom/prometheus:v2.47.1 check metrics
import sys
sys.path.insert(0, 'logging/host-health')
import collect
metrics = collect.Metrics()
metrics.add('probe_status', 2, probe='read', profile='test')
metrics.add('interface_receive_bytes_total', 123, device='eth0')
metrics.add('container_restarts_total', 2, name='long-container-name')
metrics.add('last_run_timestamp_seconds', 123456789)
print(metrics.render(), end='')
PY
sh backup/scripts/record-backup-metric.sh snapshot
sh backup/scripts/record-backup-metric.sh integrity
sh backup/scripts/record-backup-metric.sh restore fixture/project
grep -Eq '^prodenv_backup_last_success_timestamp_seconds\{operation="restore",scope="fixture/project"\} [0-9]+$' "$METRICS_DIRECTORY/backup-restore.prom"
if sh backup/scripts/record-backup-metric.sh restore '../invalid' 2>/dev/null; then
  echo 'Invalid restore scope was accepted.' >&2
  exit 1
fi
if sh backup/scripts/record-backup-metric.sh '../invalid' 2>/dev/null; then
  echo 'Invalid metric operation was accepted.' >&2
  exit 1
fi
for operation in snapshot integrity restore; do
  grep -Eq "^prodenv_backup_last_success_timestamp_seconds\{operation=\"$operation\".*\} [0-9]+$" "$METRICS_DIRECTORY/backup-$operation.prom"
  docker run --rm -i --entrypoint promtool prom/prometheus:v2.47.1 check metrics < "$METRICS_DIRECTORY/backup-$operation.prom"
done

docker run --rm --entrypoint promtool \
  -v "$PWD:/work:ro" -w /work \
  prom/prometheus:v2.47.1 test rules test/monitoring-rules.test.yml
docker run --rm --entrypoint promtool \
  -v "$PWD:/work:ro" -w /work \
  prom/prometheus:v2.47.1 test rules test/web-security-rules.test.yml
docker run --rm --entrypoint promtool \
  -v "$PWD/logging/prometheus:/etc/prometheus:ro" \
  prom/prometheus:v2.47.1 check config /etc/prometheus/prometheus.yml

export MONITORING_TEST_DIRECTORY="$temporary"
node <<'NODE'
const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
for (const environment of ['prod', 'stage']) {
  const config = fs.readFileSync(`nginx/nginx/conf.d/sinair-llm-bot-api-${environment}.conf`, 'utf8');
  const blocked = new RegExp(config.match(/location ~ (.+) \{/)[1]);
  for (const route of ['/actuator/prometheus', '/actuator/metrics', '/actuator/metrics/bot.llm.spend.usd', '/api/chat/outbound/collector-status']) assert(blocked.test(route));
  for (const route of ['/actuator/health', '/api/chat/outbound', '/oauth2/authorization/google']) assert(!blocked.test(route));
}
const dashboard = JSON.parse(fs.readFileSync('logging/grafana/provisioning/dashboards/mobile-overview.json', 'utf8'));
const flatten = panels => panels.flatMap(panel => [panel, ...flatten(panel.panels || [])]);
const panels = flatten(dashboard.panels);
assert.equal(dashboard.uid, 'mobile-overview');
assert.equal(dashboard.schemaVersion, 39);
assert.equal(dashboard.panels.filter(panel => panel.type === 'stat' && !panel.repeat).length, 4);
assert.equal(dashboard.title, 'sinair-llm-bot-prod');
assert(dashboard.panels.some(panel => panel.id === 15 && panel.type === 'stat'));
assert(dashboard.panels.some(panel => panel.id === 16 && panel.type === 'marcusolsson-dynamictext-panel'));
for (const panel of dashboard.panels.filter(panel => panel.type === 'stat' && !panel.repeat)) {
  assert.equal(panel.options.orientation, 'vertical');
  assert.equal(panel.gridPos.h, 3);
}
assert(!panels.some(panel => (panel.targets || []).some(target => /bot_queue_|node_filesystem_|node_memory_MemAvailable|prodenv_backup_/.test(target.expr))));
assert.equal(dashboard.panels.filter(panel => panel.type === 'row').length, 1);
assert(dashboard.panels.find(panel => panel.type === 'row').collapsed);
assert.equal(new Set(panels.map(panel => panel.id)).size, panels.length);
const rules = [];
const mobileDashboards = ['mobile-overview', 'mobile-project', 'mobile-server'].map(name =>
  JSON.parse(fs.readFileSync(`logging/grafana/provisioning/dashboards/${name}.json`, 'utf8')));
const projectNames = ['sinair-llm-bot-prod', 'fullstack-starter-prod', 'gentool-data-viewer-prod', 'artist-insight-service-prod'];
for (const mobile of mobileDashboards) {
  assert.deepEqual(mobile.links.map(link => link.title), ['Server', ...projectNames]);
  assert.deepEqual(mobile.links.map(link => link.url), ['/d/mobile-server', '/d/mobile-overview', ...projectNames.slice(1).map(name => `/d/mobile-project?var-project=${name}`)]);
  assert((mobile.templating.list || []).every(variable => variable.hide === 2), 'Project navigation must not use a visible dropdown');
  assert(mobile.links.every(link => link.keepTime));
  assert.equal(new Set(flatten(mobile.panels).map(panel => panel.id)).size, flatten(mobile.panels).length);
  for (const [index, first] of mobile.panels.entries()) {
    for (const second of mobile.panels.slice(index + 1)) {
      const firstBox = first.gridPos, secondBox = second.gridPos;
      assert(!(firstBox.x < secondBox.x + secondBox.w && firstBox.x + firstBox.w > secondBox.x && firstBox.y < secondBox.y + secondBox.h && firstBox.y + firstBox.h > secondBox.y), `${mobile.uid}: ${first.title} overlaps ${second.title}`);
    }
  }
  for (const panel of flatten(mobile.panels)) {
    if (panel.type === 'row') continue;
    assert.equal(panel.datasource.uid, 'Prometheus');
    if (panel.type === 'stat') assert.deepEqual(panel.options.reduceOptions.calcs, ['last']);
    for (const target of panel.targets) {
      assert(!target.expr.includes('stage'), 'Only production should be queried');
      assert(!target.expr.includes('bot_queue_'), 'No pending-work counts on mobile dashboards');
      if (mobile.uid !== 'mobile-server') {
        assert(!/node_filesystem_|node_memory_MemAvailable|prodenv_backup_/.test(target.expr), 'Server-wide metrics belong on Server');
        if (target.expr.includes('node_cpu_seconds_total')) assert(target.expr.includes('container_cpu_usage_seconds_total'), 'Only project CPU usage belongs on project views');
      }
      rules.push({record: `${mobile.uid.replace(/-/g, '_')}_${panel.id}_${target.refId}`, expr: target.expr.replace(/\$\{project:regex\}/g, 'sinair-llm-bot-prod').replace(/\$\{container:regex\}/g, '.*')});
    }
  }
}
const messages = panels.find(panel => panel.id === 4);
const projectDashboard = mobileDashboards.find(mobile => mobile.uid === 'mobile-project');
assert.equal(projectDashboard.templating.list[0].current.value, 'fullstack-starter-prod');
assert.equal(messages.type, 'stat');
assert.equal(messages.title, 'Chat messages / last 24h');
assert.deepEqual(messages.targets.map(target => target.legendFormat), ['Human messages', 'Bot replies']);
assert(messages.targets.every(target => target.instant && target.expr.includes('[24h]')));
const messageActivity = panels.find(panel => panel.id === 20);
assert.equal(messageActivity.type, 'timeseries');
assert.equal(messageActivity.title, 'Chat activity');
assert.equal(messageActivity.gridPos.y, 17);
assert.equal(messageActivity.gridPos.w, 24);
assert.equal(messageActivity.timeFrom, '24h');
assert.equal(messageActivity.hideTimeOverride, false);
assert.equal(messageActivity.maxDataPoints, 48);
assert.deepEqual(messageActivity.targets.map(target => target.legendFormat), ['Human messages', 'Bot replies']);
assert(messageActivity.targets.every(target => target.range && !target.instant && target.interval === '30m' && target.expr.includes('[30m]')));
assert(messageActivity.targets[0].expr.includes('bot_chat_human_messages_total'));
assert(messageActivity.targets[1].expr.includes('bot_replies_acknowledged_total'));
assert.equal(messageActivity.fieldConfig.defaults.custom.drawStyle, 'bars');
assert.equal(messageActivity.fieldConfig.overrides[0].matcher.options, 'Bot replies');
assert.equal(messageActivity.fieldConfig.overrides[0].properties.find(property => property.id === 'custom.drawStyle').value, 'line');
assert.deepEqual(messageActivity.options.legend.calcs, ['lastNotNull']);
assert.equal(panels.filter(panel => panel.type === 'timeseries' && panel.targets.some(target => /bot_chat_human_messages|bot_replies_acknowledged/.test(target.expr))).length, 1);
const humanMessages = messages.targets[0].expr;
const botReplies = messages.targets[1].expr;
const failures = panels.find(panel => panel.id === 10);
assert.deepEqual(failures.targets.map(target => target.legendFormat), ['Reply handling', 'Chat summaries']);
const replyTime = panels.find(panel => panel.id === 11);
assert.equal(replyTime.targets[1].legendFormat, 'Replies measured');
assert(!panels.some(panel => /BYOK/.test(panel.title)));
const spending = panels.find(panel => panel.id === 3).targets[0].expr;
const containerTable = panels.find(panel => panel.id === 16);
assert.equal(containerTable.gridPos.w, 24);
assert.equal(containerTable.gridPos.x, 0);
assert.equal(containerTable.type, 'marcusolsson-dynamictext-panel');
assert.equal(containerTable.repeat, undefined);
assert.equal(containerTable.options.renderMode, 'allRows');
assert(containerTable.options.styles.includes('grid-template-columns: repeat(2, minmax(0, 1fr))'));
assert.deepEqual(containerTable.transformations[0], {id: 'joinByField', options: {byField: 'name', mode: 'outer'}});
const helpers = {};
require('vm').runInNewContext(containerTable.options.helpers, {context: {handlebars: {registerHelper: (name, helper) => { helpers[name] = helper; }}}});
assert.equal(helpers.memory(512 * 1024 * 1024), '512 MiB');
assert.equal(helpers.memoryLimit(0), 'No cap');
assert.equal(helpers.memoryLimit(null), 'Unknown');
assert.equal(helpers.memoryLimit(NaN), 'Unknown');
assert.equal(helpers.percentage(75), '75.0%');
assert.equal(helpers.percentage(NaN), 'N/A');
assert.equal(helpers.pressure(95), 'load-critical');
assert.equal(helpers.containerName('sinair-llm-bot-prod-backend-2'), 'backend-2');
const containerQueries = Object.fromEntries(containerTable.targets.map(target => [target.refId, target.expr.replace(/\$\{container:regex\}/g, '.*')]));
const projectContainerTable = projectDashboard.panels.find(panel => panel.type === 'marcusolsson-dynamictext-panel');
assert.equal(projectContainerTable.gridPos.w, 24);
assert.equal(projectContainerTable.gridPos.x, 0);
assert.equal(projectContainerTable.repeat, undefined);
assert.deepEqual(projectContainerTable.options, containerTable.options);
assert.deepEqual(projectContainerTable.transformations, containerTable.transformations);
assert.deepEqual(projectContainerTable.fieldConfig, containerTable.fieldConfig);
assert(fs.readFileSync('logging/grafana/docker-compose.yml', 'utf8').includes('marcusolsson-dynamictext-panel 6.2.0'));
for (const target of projectContainerTable.targets) {
  assert.equal(target.expr.replace(/=~"\$\{project:regex\}"/g, '="sinair-llm-bot-prod"').replace(/\$\{container:regex\}/g, '.*'), containerQueries[target.refId]);
}
for (const mobile of [dashboard, projectDashboard]) {
  const variable = mobile.templating.list.find(variable => variable.name === 'container');
  assert(variable.multi && variable.includeAll && variable.hide === 2);
  assert(variable.query.query.includes('container_label_com_docker_compose_project'));
  const containerNamePattern = new RegExp(variable.regex.slice(1, -1).replace(/\$\{project:regex\}/g, 'sinair-llm-bot-prod'));
  for (const [name, text] of [['sinair-llm-bot-prod-backend-1', 'backend-1'], ['sinair-llm-bot-prod-backend-2', 'backend-2'], ['custom-container', 'custom-container']]) {
    const match = containerNamePattern.exec(name);
    assert.equal(match.groups.value, name);
    assert.equal(match.groups.text, text);
  }
  assert(!mobile.panels.some(panel => panel.type === 'table'));
  const history = mobile.panels.find(panel => panel.type === 'row' && panel.title === 'Diagnostics').panels.filter(panel => panel.repeat === 'container');
  assert.equal(history.length, 2);
  for (const panel of history) {
    assert.equal(panel.type, 'timeseries');
    assert.equal(panel.repeatDirection, 'v');
    assert.equal(panel.gridPos.w, 24);
    assert(panel.targets.every(target => target.range && target.expr.includes('name=~"${container:regex}"')));
  }
}
const memoryHistory = panels.find(panel => panel.id === 17).targets;
assert.equal(memoryHistory[0].expr.replace(/\$\{container:regex\}/g, '.*'), containerQueries.A);
assert.equal(panels.find(panel => panel.id === 18).targets[0].expr.replace(/\$\{container:regex\}/g, '.*'), containerQueries.B);
const memoryFixture = (name, used, limit) => [
  {series: `container_memory_working_set_bytes{job="cadvisor",name="${name}",image="test",container_label_com_docker_compose_project="sinair-llm-bot-prod"}`, values: `${used}+0x30`},
  ...(limit === undefined ? [] : [{series: `container_spec_memory_limit_bytes{job="cadvisor",name="${name}",image="test",container_label_com_docker_compose_project="sinair-llm-bot-prod"}`, values: `${limit}+0x30`}]),
];
const productionShares = mobileDashboards.find(mobile => mobile.uid === 'mobile-server').panels.find(panel => panel.id === 3);
const alertPanel = panels.find(panel => panel.id === 14);
const serverDashboard = mobileDashboards.find(mobile => mobile.uid === 'mobile-server');
const serverAlertPanel = flatten(serverDashboard.panels).find(panel => panel.id === 7);
for (const panel of flatten(serverDashboard.panels).filter(panel => panel.type === 'stat' && panel.targets.some(target => target.expr.includes('vector(0/0)')))) {
  assert(panel.fieldConfig.defaults.mappings.some(mapping => mapping.type === 'special' && mapping.options.match === 'nan'), 'Missing stat fields must not render NaN');
}
for (const panel of flatten(serverDashboard.panels).filter(panel => [11,18,37,38].includes(panel.id))) {
  assert(panel.options.afterRender.includes('getBoundingClientRect'));
  assert(panel.options.afterRender.includes('ResizeObserver'));
  new Function('context', panel.options.afterRender);
}
assert(projectDashboard.panels.some(panel => panel.id === 41 && panel.targets[0].expr.includes('project=~')));
assert(serverAlertPanel.targets[0].expr.includes('alertname!~"Bot.*"'));
assert(serverAlertPanel.targets[0].expr.includes('project!~".*-prod"'));
assert(!serverDashboard.panels.find(panel => panel.id === 1).targets.some(target => /node_cpu_seconds|node_memory_/.test(target.expr)), 'Do not repeat CPU and RAM summary panels');
assert(!flatten(serverDashboard.panels).some(panel => (panel.targets || []).some(target => /container_cpu_cfs_/.test(target.expr))), 'Container throttling belongs on projects');
for (const mobile of [dashboard, projectDashboard]) {
  const health = flatten(mobile.panels).find(panel => panel.id === 40);
  assert(health.targets.every(target => target.expr.includes('project=') || target.expr.includes('project=~')));
  assert(health.options.styles.includes('overflow-wrap:anywhere'));
  assert(flatten(mobile.panels).some(panel => (panel.targets || []).some(target => target.expr.includes('container_cpu_cfs_throttled_periods_total'))));
}
const alertHistory = dashboard.panels.find(panel => panel.id === 19);
const serverAlertHistory = serverDashboard.panels.find(panel => panel.id === 9);
assert.equal(alertPanel.type, 'marcusolsson-dynamictext-panel');
assert.equal(serverAlertPanel.type, 'marcusolsson-dynamictext-panel');
assert.deepEqual(alertPanel.options, serverAlertPanel.options);
assert(dashboard.panels.includes(alertPanel), 'Bot alerts must not be hidden in Diagnostics');
assert(mobileDashboards.find(mobile => mobile.uid === 'mobile-server').panels.includes(serverAlertPanel));
assert.equal(alertPanel.gridPos.y, 3);
assert.equal(serverAlertPanel.gridPos.y, 3);
assert(!JSON.stringify(alertPanel.options).includes('viewPanel'));
for (const history of [alertHistory, serverAlertHistory]) {
  assert.equal(history.type, 'state-timeline');
  assert.equal(history.title, 'Alerts / 24h');
  assert.equal(history.gridPos.y, history === alertHistory ? 32 : 25);
  assert.equal(history.gridPos.w, 24);
  assert.equal(history.timeFrom, '24h');
  assert.equal(history.hideTimeOverride, true);
  assert.equal(history.targets.length, 1);
  assert.equal(history.targets[0].range, true);
  assert.equal(history.targets[0].instant, undefined);
  assert.equal(history.targets[0].legendFormat, '{{display}} / {{affected}}');
  assert(history.targets[0].expr.includes('ALERTS{alertstate="firing"'));
  assert(history.targets[0].expr.includes('"affected"'));
  assert.equal(history.options.mergeValues, true);
  assert.equal(history.options.showValue, 'never');
  assert.equal(history.fieldConfig.defaults.mappings[0].options['1'].text, 'Firing');
  assert.equal(history.fieldConfig.defaults.custom.axisWidth, 200);
  assert.equal(history.transformations, undefined);
  assert(history.targets[0].expr.includes('"display"'));
  assert(history.description.includes('gap means resolved or no sample'));
}
assert(alertHistory.targets[0].expr.includes('alertname=~"Bot.*"'));
assert(!serverAlertHistory.targets[0].expr.includes('alertname=~"Bot.*"'));
assert.equal(dashboard.panels.find(panel => panel.id === 9).gridPos.y, 38);
assert(!mobileDashboards.some(mobile => flatten(mobile.panels).some(panel => panel.type === 'table')));
assert(alertPanel.options.styles.includes('overflow-wrap: anywhere'));
assert(!/ellipsis|line-clamp/.test(alertPanel.options.styles));
assert(!alertPanel.options.content.includes('<table'));
const alertHelpers = {};
require('vm').runInNewContext(alertPanel.options.helpers, {context: {handlebars: {registerHelper: (name, helper) => { alertHelpers[name] = helper; }}}});
assert.equal(alertHelpers.alertTitle('ContainerCPUThrottling'), 'Container CPU Throttling');
assert.equal(alertHelpers.severityClass('critical'), 'severity-critical');
assert.equal(alertHelpers.severityClass('unexpected'), 'severity-unknown');
const longTarget = 'sinair-llm-bot-prod-backend-' + 'long-name-'.repeat(20);
const details = alertHelpers.alertDetails({alertname: 'BotWorkStuck', severity: 'warning', name: longTarget, instance: 'backend:8080', demo: 'sample', Time: 10, Value: 1, 'Value #A': 1});
assert.equal(details.length, 2);
assert.equal(details[0].label, 'Container');
assert.equal(details[0].value, longTarget);
assert.equal(alertHelpers.alertRows([{alertname: 'Warning', severity: 'warning'}, {alertname: 'Critical', severity: 'critical'}])[0].alertname, 'Critical');
assert.equal(alertHelpers.alertRows(null).length, 0);
const composeDetails = alertHelpers.alertDetails({container_label_com_docker_compose_service: 'backend', container_label_com_docker_compose_project: 'sinair-llm-bot-prod', container_label_com_docker_compose_container_number: '1'});
assert.equal(composeDetails[0].label, 'Service');
assert.equal(composeDetails[0].value, 'backend');
assert.equal(composeDetails[1].label, 'Project');
assert.equal(composeDetails[2].label, 'Replica');
const manyAlerts = [{alertname: 'Warning A', severity: 'warning'}, {alertname: 'Warning B', severity: 'warning'}, {alertname: 'Critical', severity: 'critical'}, {alertname: 'Info', severity: 'info'}];
assert.equal(alertHelpers.alertRows(manyAlerts).length, 4);
assert.equal(alertHelpers.alertRows(manyAlerts)[0].alertname, 'Critical');
assert.equal(alertHelpers.alertCount(manyAlerts), 4);
assert.equal(alertHelpers.alertSeveritySummary(manyAlerts), '1 critical, 2 warning, 1 info');
assert.equal(alertHelpers.alertSeveritySummary([{alertname: 'Unknown', severity: 'unexpected'}]), '1 unknown severity');
assert.equal(alertHelpers.alertSeveritySummary(null), '');
assert.equal(alertHelpers.alertCount([]), 0);
assert.equal(alertHelpers.alertRows([{alertname: 'Only alert'}]).length, 1);
assert(alertPanel.options.content.includes('each (alertRows data)'));
assert(alertPanel.options.content.includes('Expand details'));
assert(!alertPanel.options.content.includes('href='));
const style = initial => {
  const values = new Map(Object.entries(initial || {}).map(([key, value]) => [key, [value, '']]));
  return {
    get height() { return this.getPropertyValue('height'); },
    getPropertyValue: key => (values.get(key) || ['', ''])[0],
    getPropertyPriority: key => (values.get(key) || ['', ''])[1],
    setProperty: (key, value, priority) => values.set(key, [value, priority]),
    removeProperty: key => values.delete(key),
  };
};
const detailElements = [{hidden: true}, {hidden: true}];
const listeners = new Map();
const attributes = new Map();
const toggleButton = {addEventListener: (event, callback) => listeners.set(event, callback), removeEventListener: event => listeners.delete(event), setAttribute: (key, value) => attributes.set(key, value)};
const grid = {style: style({height: '600px'})};
const panel = {style: style({height: '296px'}), parentElement: grid, isConnected: true, getBoundingClientRect: () => ({top: 10})};
const sibling = {style: style(), classList: {contains: name => name === 'react-grid-item'}, getBoundingClientRect: () => ({top: 314 + (parseFloat(sibling.style.getPropertyValue('--inline-alert-offset')) || 0)})};
grid.children = [panel, sibling];
const wrapper = {style: style({height: '262px', overflow: 'auto'}), parentElement: panel};
const list = {getBoundingClientRect: () => ({bottom: 43 + (detailElements[0].hidden ? 300 : 760)})};
const root = {parentElement: wrapper, closest: () => panel, querySelector: selector => selector === '.alert-list' ? list : toggleButton, querySelectorAll: () => detailElements};
const frames = new Map();
let nextFrame = 0;
const observers = [];
class Observer {
  constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
  observe() {}
  disconnect() { this.disconnected = true; }
}
const flushFrames = () => { const pending = Array.from(frames.values()); frames.clear(); pending.forEach(callback => callback()); };
let positioning = 'absolute';
const attach = require('vm').runInNewContext('(function () {' + alertPanel.options.afterRender + '\n})', {
  context: {element: root}, ResizeObserver: Observer, MutationObserver: Observer,
  getComputedStyle: () => ({position: positioning}),
  requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; }, cancelAnimationFrame: id => frames.delete(id),
});
const state = {};
let cleanup = attach.call(state);
flushFrames();
assert.equal(panel.style.getPropertyValue('min-height'), '334px');
assert.equal(sibling.style.getPropertyValue('--inline-alert-offset'), '38px');
assert.equal(grid.style.getPropertyValue('min-height'), '638px');
assert.equal(attributes.get('aria-expanded'), 'false');
listeners.get('click')();
flushFrames();
assert(detailElements.every(detail => !detail.hidden));
assert.equal(panel.style.getPropertyValue('min-height'), '794px');
assert.equal(sibling.style.getPropertyValue('--inline-alert-offset'), '498px');
assert.equal(attributes.get('aria-expanded'), 'true');
observers[1].callback();
flushFrames();
assert.equal(sibling.style.getPropertyValue('--inline-alert-offset'), '498px', 'Reflow must not accumulate offsets');
cleanup();
assert(observers.every(observer => observer.disconnected));
assert.equal(listeners.size, 0);
assert.equal(wrapper.style.height, '262px');
assert.equal(wrapper.style.getPropertyValue('overflow'), 'auto');
assert.equal(sibling.style.getPropertyValue('translate'), '');
assert.equal(grid.style.getPropertyValue('min-height'), '');
cleanup = attach.call(state);
flushFrames();
assert.equal(attributes.get('aria-expanded'), 'true', 'Refresh must preserve expanded details');
listeners.get('click')();
flushFrames();
assert.equal(panel.style.getPropertyValue('min-height'), '334px');
assert.equal(attributes.get('aria-expanded'), 'false');
cleanup();
positioning = 'static';
cleanup = attach.call(state);
flushFrames();
assert.equal(panel.style.getPropertyValue('min-height'), '334px');
assert.equal(sibling.style.getPropertyValue('--inline-alert-offset'), '0px', 'Mobile flow already moves subsequent panels');
assert.equal(grid.style.getPropertyValue('max-height'), 'none');
listeners.get('click')();
flushFrames();
assert.equal(panel.style.getPropertyValue('min-height'), '794px');
assert.equal(sibling.style.getPropertyValue('--inline-alert-offset'), '0px');
cleanup();
assert.equal(productionShares.type, 'marcusolsson-dynamictext-panel');
assert.equal(productionShares.title, 'Production share of server');
assert.equal(productionShares.gridPos.w, 24);
assert(productionShares.options.styles.includes('grid-template-columns: repeat(2, minmax(0, 1fr))'));
assert(productionShares.options.content.includes('{{project}}'));
assert(productionShares.options.content.includes('CPU / server'));
assert(productionShares.options.content.includes('RAM / server'));
assert.deepEqual(productionShares.transformations[0], {id: 'joinByField', options: {byField: 'container_label_com_docker_compose_project', mode: 'outer'}});
const projectHelpers = {};
require('vm').runInNewContext(productionShares.options.helpers, {context: {handlebars: {registerHelper: (name, helper) => { projectHelpers[name] = helper; }}}});
assert.equal(projectHelpers.share(25), '25.0%');
assert.equal(projectHelpers.share(null), 'Unknown');
assert.equal(projectHelpers.share(NaN), 'Unknown');
assert.equal(projectHelpers.memory(512 * 1024 * 1024), '512 MiB');
fs.writeFileSync(path.join(process.env.MONITORING_TEST_DIRECTORY, 'dashboard-rules.json'), JSON.stringify({groups: [{name: 'dashboard', rules}]}));
fs.writeFileSync(path.join(process.env.MONITORING_TEST_DIRECTORY, 'dashboard-tests.json'), JSON.stringify({
  rule_files: [], evaluation_interval: '1m', tests: [
    {interval: '1m', input_series: [
      {series: 'up{job="cadvisor"}', values: '1+0x30'},
      {series: 'up{job="node"}', values: '1+0x30'},
      {series: 'node_memory_MemTotal_bytes{job="node"}', values: '4096+0x30'},
      {series: 'node_cpu_seconds_total{job="node",mode="idle",cpu="0"}', values: '0+30x30'},
      {series: 'node_cpu_seconds_total{job="node",mode="idle",cpu="1"}', values: '0+30x30'},
      ...memoryFixture('app-1', 512, 1024),
      ...memoryFixture('app-2', 512, 1024),
      {series: 'container_memory_working_set_bytes{job="cadvisor",name="app-1",copy="duplicate",image="test",container_label_com_docker_compose_project="sinair-llm-bot-prod"}', values: '512+0x30'},
      {series: 'container_cpu_usage_seconds_total{job="cadvisor",name="app-1",image="test",container_label_com_docker_compose_project="sinair-llm-bot-prod"}', values: '0+15x30'},
      {series: 'container_cpu_usage_seconds_total{job="cadvisor",name="app-2",image="test",container_label_com_docker_compose_project="sinair-llm-bot-prod"}', values: '0+15x30'},
      {series: 'container_memory_working_set_bytes{job="cadvisor",name="stage",image="test",container_label_com_docker_compose_project="sinair-llm-bot-stage"}', values: '2048+0x30'},
      {series: 'container_memory_working_set_bytes{job="cadvisor",name="monitoring",image="test",container_label_com_docker_compose_project="prodenv"}', values: '1024+0x30'},
      {series: 'container_cpu_usage_seconds_total{job="cadvisor",name="stage",image="test",container_label_com_docker_compose_project="sinair-llm-bot-stage"}', values: '0+30x30'},
    ], promql_expr_test: [
      {expr: productionShares.targets[0].expr, eval_time: '30m', exp_samples: [{labels: '{container_label_com_docker_compose_project="sinair-llm-bot-prod"}', value: 1024}]},
      {expr: productionShares.targets[1].expr, eval_time: '30m', exp_samples: [{labels: '{container_label_com_docker_compose_project="sinair-llm-bot-prod"}', value: 25}]},
      {expr: productionShares.targets[2].expr, eval_time: '30m', exp_samples: [{labels: '{container_label_com_docker_compose_project="sinair-llm-bot-prod"}', value: 25}]},
    ]},
    {interval: '1m', input_series: [
      {series: 'up{job="cadvisor"}', values: '0+0x30'},
      {series: 'up{job="node"}', values: '1+0x30'},
      ...memoryFixture('stale', 512, 1024),
    ], promql_expr_test: [{expr: productionShares.targets[0].expr, eval_time: '30m', exp_samples: []}]},
    {interval: '1m', input_series: [
      {series: 'up{job="cadvisor"}', values: '1+0x30'},
      {series: 'up{job="node"}', values: '1+0x30'},
      {series: 'node_memory_MemTotal_bytes{job="node"}', values: '8192+0x30'},
      ...memoryFixture('bounded', 384, 512),
      ...memoryFixture('replica', 128, 256),
      ...memoryFixture('no-limit', 100, 0),
      ...memoryFixture('host-limit', 200, 8192),
      ...memoryFixture('unlimited-v1', 300, '9223372036854771712'),
      ...memoryFixture('missing-limit', 400),
    ], promql_expr_test: [
      {expr: `(${containerQueries.C}) == (${containerQueries.C})`, eval_time: '30m', exp_samples: [
        {labels: '{name="bounded"}', value: 512}, {labels: '{name="replica"}', value: 256},
        {labels: '{name="no-limit"}', value: 0}, {labels: '{name="host-limit"}', value: 0}, {labels: '{name="unlimited-v1"}', value: 0},
      ]},
      {expr: `(${containerQueries.D}) == (${containerQueries.D})`, eval_time: '30m', exp_samples: [
        {labels: '{name="bounded"}', value: 75}, {labels: '{name="replica"}', value: 50},
      ]},
      {expr: 'count(' + containerQueries.A + ')', eval_time: '30m', exp_samples: [{labels: '{}', value: 6}]},
      {expr: `count((${containerQueries.C}) != (${containerQueries.C}))`, eval_time: '30m', exp_samples: [{labels: '{}', value: 1}]},
      {expr: `count((${containerQueries.D}) != (${containerQueries.D}))`, eval_time: '30m', exp_samples: [{labels: '{}', value: 4}]},
    ]},
    {interval: '1m', input_series: [
      ...[['5', 10], ['15', 60], ['30', 95], ['60', 100], ['120', 100], ['300', 100], ['900', 100], ['+Inf', 100]].map(([le, count]) => ({
        series: `bot_reply_ack_latency_seconds_bucket{job="sinair-llm-bot-prod",le="${le}"}`,
        values: `0 ${count}+0x29`,
      })),
      {series: 'bot_reply_ack_latency_seconds_count{job="sinair-llm-bot-prod"}', values: '0 100+0x29'},
      {series: 'bot_pipeline_runs_total{job="sinair-llm-bot-prod",outcome="failed"}', values: '0 3+0x29'},
      {series: 'bot_pipeline_runs_total{job="sinair-llm-bot-prod",outcome="summary_failed"}', values: '0 1+0x29'},
    ], promql_expr_test: [
      {expr: replyTime.targets[0].expr, eval_time: '30m', exp_samples: [{labels: '{}', value: 30}]},
      {expr: replyTime.targets[1].expr, eval_time: '30m', exp_samples: [{labels: '{}', value: 100}]},
      {expr: failures.targets[0].expr, eval_time: '30m', exp_samples: [{labels: '{}', value: 3}]},
      {expr: failures.targets[1].expr, eval_time: '30m', exp_samples: [{labels: '{}', value: 1}]},
    ]},
    {interval: '1m', input_series: [], promql_expr_test: [
      {expr: humanMessages, eval_time: '30m', exp_samples: []},
      {expr: botReplies, eval_time: '30m', exp_samples: []},
    ]},
    {interval: '1m', input_series: [
      {series: 'bot_chat_human_messages_total{job="sinair-llm-bot-prod"}', values: '0+10x10 0+10x19'},
    ], promql_expr_test: [{expr: humanMessages, eval_time: '30m', exp_samples: [{labels: '{}', value: 290}]}]},
    {interval: '1m', input_series: [
      {series: 'bot_chat_human_messages_total{job="sinair-llm-bot-prod"}', values: '0+10x30'},
      {series: 'bot_replies_acknowledged_total{job="sinair-llm-bot-prod"}', values: '0+1x30'},
    ], promql_expr_test: [
      {expr: humanMessages, eval_time: '30m', exp_samples: [{labels: '{}', value: 300}]},
      {expr: botReplies, eval_time: '30m', exp_samples: [{labels: '{}', value: 30}]},
    ]},
    {interval: '1m', input_series: [
      {series: 'bot_chat_human_messages_total{job="sinair-llm-bot-prod"}', values: '0+0x30'},
      {series: 'bot_replies_acknowledged_total{job="sinair-llm-bot-prod"}', values: '0+0x30'},
      {series: 'bot_llm_spend_usd{job="sinair-llm-bot-prod",source="openrouter",period="day"}', values: '12+0x30'},
      {series: 'bot_llm_usage_timestamp_seconds{job="sinair-llm-bot-prod"}', values: '1+0x30'},
      {series: 'up{job="sinair-llm-bot-prod"}', values: '1+0x30'},
    ], promql_expr_test: [
      {expr: humanMessages, eval_time: '30m', exp_samples: [{labels: '{}', value: 0}]},
      {expr: botReplies, eval_time: '30m', exp_samples: [{labels: '{}', value: 0}]},
      {expr: spending, eval_time: '30m', exp_samples: []},
    ]},
  ],
}));
console.log(`Dashboards: ${mobileDashboards.length} consistent views, ${rules.length} production-only queries`);
if (process.env.MONITORING_PREVIEW_URL) {
  const http = require('http');
  const fetchDashboard = uid => new Promise((resolve, reject) => {
    const request = http.get(`${process.env.MONITORING_PREVIEW_URL}/api/dashboards/uid/${uid}`, response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('error', reject);
      response.on('end', () => {
        try {
          assert.equal(response.statusCode, 200);
          resolve(JSON.parse(body).dashboard);
        } catch (error) { reject(error); }
      });
    });
    request.on('error', reject);
    request.setTimeout(5000, () => request.destroy(new Error('Preview request timed out')));
  });
  const contract = dashboard => ({
    title: dashboard.title,
    links: dashboard.links.map(link => ({title: link.title, url: link.url})),
    panels: flatten(dashboard.panels).map(panel => ({
      id: panel.id, title: panel.title, type: panel.type, gridPos: panel.gridPos,
      timeFrom: panel.timeFrom, hideTimeOverride: panel.hideTimeOverride,
      timelineOptions: panel.type === 'state-timeline' ? panel.options : undefined,
      timelineFields: panel.type === 'state-timeline' ? panel.fieldConfig : undefined,
      transformations: panel.transformations,
      cardTemplate: panel.type === 'marcusolsson-dynamictext-panel' ? panel.options.content : undefined,
      cardStyles: panel.type === 'marcusolsson-dynamictext-panel' ? panel.options.styles : undefined,
      targets: (panel.targets || []).map(target => ({expr: target.expr, legendFormat: target.legendFormat})),
    })),
  });
  Promise.all(mobileDashboards.map(async source => {
    assert.deepEqual(contract(await fetchDashboard(source.uid)), contract(source), `${source.uid}: preview differs from workspace`);
  })).then(() => console.log('Live Grafana matches all workspace dashboards.'))
    .catch(error => { console.error(error); process.exitCode = 1; });
}
NODE
docker run --rm --entrypoint promtool -v "$temporary:/checks:ro" \
  prom/prometheus:v2.47.1 check rules /checks/dashboard-rules.json
docker run --rm --entrypoint promtool -v "$temporary:/checks:ro" \
  prom/prometheus:v2.47.1 test rules /checks/dashboard-tests.json

echo 'Backup metric publication and monitoring rules passed.'