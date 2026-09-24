#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PROMTOOL="${PROMTOOL:-promtool}"

for executable in docker node "$PROMTOOL"; do
  command -v "$executable" >/dev/null || { printf 'Required executable missing: %s\n' "$executable" >&2; exit 1; }
done

node <<'NODE'
const assert = require('assert').strict;
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execFileSync} = require('child_process');

const root = process.cwd();
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'prodenv-resource-check-'));
const fixture = path.join(temporary, 'fixture');
const promtool = process.env.PROMTOOL;

function run(command, args, options = {}) {
  return execFileSync(command, args, {encoding: 'utf8', ...options});
}

function compose(files, extra = [], environment = {}) {
  return JSON.parse(run('docker', [
    'compose', '--project-name', 'resource-check',
    '--project-directory', fixture,
    ...files.flatMap(file => ['-f', path.join(fixture, file)]),
    'config', '--no-env-resolution', '--format', 'json', ...extra,
  ], {env: {...process.env, ...environment}}));
}

function checkLimits(services) {
  for (const [name, service] of Object.entries(services)) {
    assert(Number(service.mem_limit) > 0, `${name}: missing RAM limit`);
    assert(Number(service.memswap_limit) >= Number(service.mem_limit), `${name}: invalid RAM + swap limit`);
    assert(Number(service.cpus) > 0 && Number(service.cpus) <= 4, `${name}: invalid CPU limit`);
    assert(Number(service.pids_limit) > 0, `${name}: missing PID limit`);
  }
}

function writeJson(name, value) {
  const filename = path.join(temporary, name);
  fs.writeFileSync(filename, JSON.stringify(value));
  return filename;
}

try {
  const workspaceFiles = run('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'])
    .split('\0').filter(filename => filename && fs.existsSync(filename));
  const yamlFiles = workspaceFiles.filter(filename => /\.ya?ml$/.test(filename));
  for (const filename of new Set(yamlFiles)) {
    const destination = path.join(fixture, filename);
    fs.mkdirSync(path.dirname(destination), {recursive: true});
    fs.copyFileSync(path.join(root, filename), destination);
    let directory = path.dirname(destination);
    while (directory === fixture || directory.startsWith(fixture + path.sep)) {
      fs.writeFileSync(path.join(directory, '.env'), '');
      directory = path.dirname(directory);
    }
  }

  const infrastructure = compose(['docker-compose.yml']);
  checkLimits(infrastructure.services);
  assert(!infrastructure.services.cadvisor.ports, 'cAdvisor must not publish a host port');
  const totalMiB = Object.values(infrastructure.services)
    .reduce((total, service) => total + Number(service.mem_limit), 0) / 1024 / 1024;
  console.log(`Infrastructure: ${Object.keys(infrastructure.services).length} bounded services; ${totalMiB} MiB combined RAM ceilings (not reservations)`);

  const overrides = yamlFiles.filter(filename => /^cicd\/docker-webhook\/shared\/envs\/[^/]+\/docker-compose\.override\.yml$/.test(filename));
  let applicationServices = 0;
  for (const filename of overrides) {
    const config = compose([filename], ['--no-consistency']);
    checkLimits(config.services);
    applicationServices += Object.keys(config.services).length;
    const tuned = compose([filename], ['--no-consistency'], {
      BACKEND_MEMORY_LIMIT: '896m', BACKEND_MEMORY_SWAP_LIMIT: '1152m',
      BACKEND_CPU_LIMIT: '1.5', BACKEND_PIDS_LIMIT: '640',
    });
    const backend = tuned.services.app || tuned.services.backend;
    assert.equal(Number(backend.mem_limit), 896 * 1024 * 1024);
    assert.equal(Number(backend.memswap_limit), 1152 * 1024 * 1024);
    assert.equal(Number(backend.cpus), 1.5);
    assert.equal(Number(backend.pids_limit), 640);
  }
  assert.equal(overrides.length, 8);
  console.log(`Applications: ${overrides.length} overrides, ${applicationServices} bounded services; tuning variables verified`);

  const dashboardSources = [{id: 1860, revision: 37}, {id: 19792, revision: 6}];
  const expressions = [];
  const dashboardUids = new Set();
  let panelCount = 0;
  function collectPanels(panels) {
    return panels.flatMap(panel => [panel, ...collectPanels(panel.panels || [])]);
  }
  function checkDatasourceReferences(value, variable) {
    if (!value || typeof value !== 'object') return;
    if (value.datasource && value.datasource.type === 'prometheus') {
      assert.equal(value.datasource.uid, '${' + variable + '}');
    }
    Object.values(value).forEach(child => checkDatasourceReferences(child, variable));
  }
  for (const source of dashboardSources) {
    const filename = `logging/grafana/provisioning/dashboards/dashboard-${source.id}.json`;
    assert(workspaceFiles.includes(filename), `Dashboard missing or ignored by Git: ${filename}`);
    const dashboard = JSON.parse(fs.readFileSync(filename, 'utf8'));
    assert.deepEqual(dashboard['x-upstream'], {
      id: source.id, revision: source.revision,
      url: `https://grafana.com/api/dashboards/${source.id}/revisions/${source.revision}/download`,
    });
    assert(dashboard.uid && !dashboardUids.has(dashboard.uid), 'Missing or duplicate dashboard UID');
    dashboardUids.add(dashboard.uid);
    assert.equal(dashboard.id, null);
    assert.equal(dashboard.refresh, '1m');
    assert(dashboard.schemaVersion <= 39, 'Dashboard schema exceeds Grafana 11.6');
    const datasources = dashboard.templating.list.filter(variable => variable.type === 'datasource');
    assert.equal(datasources.length, 1);
    assert.equal(datasources[0].current.value, 'Prometheus');
    checkDatasourceReferences(dashboard, datasources[0].name);
    const panels = collectPanels(dashboard.panels);
    assert.equal(new Set(panels.map(panel => panel.id)).size, panels.length);
    panelCount += panels.length;
    const variables = {
      __rate_interval: '5m', job: 'node', node: 'node-exporter:9100', diskdevices: '.*',
      host_instance: 'cadvisor:8080', compose_project: 'prodenv', container_name: '.*',
    };
    for (const panel of panels) {
      for (const target of panel.targets || []) {
        if (!target.expr) continue;
        expressions.push(target.expr.replace(/\$\{(\w+)(?::[^}]*)?\}|\$(\w+)/g, (match, braced, plain) => {
          const name = braced || plain;
          assert(Object.prototype.hasOwnProperty.call(variables, name), `Unknown query variable: ${match}`);
          return variables[name];
        }));
      }
    }
    console.log(`Dashboard ${source.id} revision ${source.revision}: ${panels.length} panels/rows; datasource bindings verified`);
  }
  const dashboardRules = writeJson('dashboard-rules.json', {groups: [{name: 'dashboard-validation', rules:
    expressions.map((expr, index) => ({record: `resource_dashboard_${index}`, expr})),
  }]});
  run(promtool, ['check', 'rules', dashboardRules], {stdio: 'inherit'});
  run(promtool, ['check', 'config', '--syntax-only', 'logging/prometheus/prometheus.yml'], {stdio: 'inherit'});
  run(promtool, ['check', 'rules', 'logging/prometheus/resource-alerts.yml'], {stdio: 'inherit'});

  const hostTotal = {series: 'node_memory_MemTotal_bytes{job="node",instance="host"}', values: '8589934592+0x30'};
  const hostAvailable = {series: 'node_memory_MemAvailable_bytes{job="node",instance="host"}', values: '1717986918+0x30'};
  const boundedLimit = {series: 'container_spec_memory_limit_bytes{job="cadvisor",image="java",name="bounded"}', values: '268435456+0x30'};
  const boundedWorkingSet = {series: 'container_memory_working_set_bytes{job="cadvisor",image="java",name="bounded"}', values: '255013683+0x30'};
  const unlimitedNames = ['unlimited-host', 'unlimited-zero', 'unlimited-v1'];
  const unlimitedLimits = ['8589934592', '0', '9223372036854771712'];
  const unlimitedSeries = unlimitedNames.flatMap((name, index) => [
    {series: `container_spec_memory_limit_bytes{job="cadvisor",image="java",name="${name}"}`, values: `${unlimitedLimits[index]}+0x30`},
    {series: `container_memory_working_set_bytes{job="cadvisor",image="java",name="${name}"}`, values: '255013683+0x30'},
  ]);
  const healthyScrapes = ['node', 'cadvisor'].map(job => ({series: `up{job="${job}"}`, values: '1+0x30'}));
  const alertTests = writeJson('alert-tests.json', {
    rule_files: [path.join(root, 'logging/prometheus/resource-alerts.yml')],
    evaluation_interval: '1m',
    tests: [
      {
        interval: '1m',
        input_series: [hostTotal, hostAvailable, boundedLimit, boundedWorkingSet, ...unlimitedSeries, ...healthyScrapes],
        promql_expr_test: [
          {expr: 'count by (name) (ALERTS{alertname="ContainerNearMemoryLimit",alertstate="firing"})', eval_time: '20m', exp_samples: [{labels: '{name="bounded"}', value: 1}]},
          {expr: 'count by (name) (ALERTS{alertname="ContainerWithoutMemoryLimit",alertstate="firing"})', eval_time: '20m', exp_samples: unlimitedNames.map(name => ({labels: `{name="${name}"}`, value: 1}))},
          {expr: 'ALERTS{alertname="HostMemoryPressure",alertstate="firing"}', eval_time: '20m', exp_samples: []},
          {expr: 'ALERTS{alertname=~"HostMetricsUnavailable|ContainerMetricsUnavailable",alertstate="firing"}', eval_time: '20m', exp_samples: []},
        ],
      },
      {
        interval: '1m',
        input_series: [hostTotal, ...healthyScrapes,
          {series: hostAvailable.series, values: '429496729+0x30'},
          {series: 'node_vmstat_pswpin{job="node",instance="host"}', values: '0+1920x30'},
        ],
        promql_expr_test: [
          {expr: 'count(ALERTS{alertname="HostMemoryPressure",alertstate="firing"})', eval_time: '20m', exp_samples: [{labels: '{}', value: 1}]},
          {expr: 'count(ALERTS{alertname="HostSwapChurn",alertstate="firing"})', eval_time: '20m', exp_samples: [{labels: '{}', value: 1}]},
        ],
      },
      {
        interval: '1m', input_series: [],
        promql_expr_test: [
          {expr: 'count(ALERTS{alertname=~"HostMetricsUnavailable|ContainerMetricsUnavailable",alertstate="firing"})', eval_time: '6m', exp_samples: [{labels: '{}', value: 2}]},
        ],
      },
    ],
  });
  run(promtool, ['test', 'rules', alertTests], {stdio: 'inherit'});
  console.log(`Resource validation passed: ${dashboardSources.length} pinned dashboards, ${panelCount} panels/rows, ${expressions.length} queries, 3 alert scenarios`);
} finally {
  if (fs.rmSync) {
    fs.rmSync(temporary, {recursive: true, force: true});
  } else {
    fs.rmdirSync(temporary, {recursive: true});
  }
}
NODE