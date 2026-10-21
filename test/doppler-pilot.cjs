const assert = require('assert').strict;
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execFileSync} = require('child_process');

const checkout = process.argv[2];
assert(checkout, 'Pass the fullstack-starter checkout path');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'doppler-pilot-'));
const template = path.join(checkout, 'templates/docker');

function copyConfiguration(directory, relative = '') {
  for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
    const filename = path.join(relative, entry.name);
    if (entry.isDirectory()) copyConfiguration(path.join(directory, entry.name), filename);
    else if (/\.ya?ml$/.test(entry.name)) {
      const destination = path.join(temporary, filename);
      fs.mkdirSync(path.dirname(destination), {recursive: true});
      fs.copyFileSync(path.join(directory, entry.name), destination);
    }
  }
}

function compose(credentials, extra = [], deployment = 'fullstack-starter-stage') {
  return JSON.parse(execFileSync('docker', ['compose', '--env-file', '/dev/null',
    '--project-directory', temporary, '-f', path.join(temporary, 'docker-compose.yml'),
    ...extra.flatMap(filename => ['-f', path.join(temporary, filename)]),
    '--profile', 'secrets-bootstrap', 'config', '--format', 'json'], {encoding: 'utf8', env: {
      ...process.env, COMPOSE_PROJECT_NAME: deployment,
      COMPOSE_CREDENTIALS_FILE: credentials, SPRING_PROFILES_ACTIVE: 'stage', FRONTEND_PROFILE: 'stage',
      SECRET_DIRECTORY: path.join(temporary, 'secrets'),
      POSTGRES_USER: 'fixture_admin', POSTGRES_PASSWORD: 'fixture_admin_password',
      POSTGRES_APP_USER: 'fixture_app', POSTGRES_APP_PASSWORD: 'fixture_app_password',
      GOOGLE_CLIENT_ID: 'fixture_client', GOOGLE_CLIENT_SECRET: 'fixture_google_secret',
    }}));
}

try {
  copyConfiguration(template);
  const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, '../credentials/doppler/catalog.json'), 'utf8'));
  assert.equal(catalog.file_delivery_adapters.length, 2);
  const legacy = compose('credentials-env.yml');
  assert.equal(legacy.services.db.environment.POSTGRES_PASSWORD, 'fixture_admin_password');
  assert.equal(legacy.services.backend.environment.POSTGRES_APP_PASSWORD, 'fixture_app_password');
  assert.equal(legacy.services.flyway.environment.POSTGRES_PASSWORD, 'fixture_admin_password');
  assert(legacy.services.flyway.volumes.some(volume => volume.source === path.join(temporary, 'flyway/flyway.conf')));
  console.log('Pilot legacy credential routing passed');
  const files = compose('credentials-files.yml', ['docker-compose.prodenv.yml', 'docker-compose.secrets.yml']);
  for (const service of Object.values(files.services)) {
    assert(!service.env_file, 'File credentials must not load environment files');
    for (const [key, value] of Object.entries(service.environment || {})) {
      assert(!/fixture_(admin_password|app_password|google_secret)/.test(String(value)), `${key}: credential leaked into Compose`);
      assert(!['POSTGRES_PASSWORD', 'POSTGRES_APP_PASSWORD', 'GOOGLE_CLIENT_SECRET', 'PGPASSWORD'].includes(key));
    }
  }
  assert.equal(files.services.backend.environment.SPRING_CONFIG_IMPORT, 'configtree:/run/secrets/');
  assert.equal(files.services['postgres-export'].environment.PGUSER, 'starter_backup');
  assert.equal(files.services['postgres-export'].environment.BACKUP_INCLUDE_ROLE_PASSWORDS, 'false');
  assert.equal(files.services.db.environment.POSTGRES_PASSWORD_FILE, '/run/secrets/password');
  for (const name of ['secrets-migrate', 'secrets-provision']) {
    assert.equal(files.services[name].logging.driver, 'none');
    assert.deepEqual(files.services[name].profiles, ['secrets-bootstrap']);
  }
  for (const name of ['db', 'backend', 'flyway', 'postgres-export']) {
    const mounts = files.services[name].volumes.filter(volume => volume.source.startsWith(path.join(temporary, 'secrets')));
    assert.equal(mounts.length, 1, `${name}: expected one restricted secret mount`);
    assert(mounts.every(volume => volume.read_only && (volume.bind || {}).create_host_path !== true));
    assert(!mounts.some(volume => volume.source.includes('/bootstrap')));
  }
  console.log('Pilot file credentials and consumer isolation passed');
  for (const environment of ['stage', 'prod']) {
    const deployment = `fullstack-starter-${environment}`;
    const override = `host-${environment}.yml`;
    fs.copyFileSync(path.join(__dirname, '../cicd/docker-webhook/shared/envs', deployment, 'docker-compose.override.yml'), path.join(temporary, override));
    const stack = [override, 'docker-compose.prodenv.yml'];
    const previous = compose('credentials-env.yml', stack, deployment);
    const pilot = compose('credentials-files.yml', stack, deployment);
    for (const name of ['db', 'flyway', 'backend', 'frontend', 'postgres-export']) {
      for (const limit of ['cpus', 'mem_limit', 'memswap_limit', 'pids_limit']) {
        assert(Number(pilot.services[name][limit]) > 0, `${deployment}/${name}: missing ${limit}`);
        assert.equal(pilot.services[name][limit], previous.services[name][limit], `${deployment}/${name}: ${limit} changed with credential mode`);
      }
    }
  }
  console.log('Pilot stage/prod resource and host-overlay compatibility passed');
  const infrastructure = path.join(temporary, 'infrastructure');
  const repository = path.join(__dirname, '..');
  const yamlFiles = execFileSync('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'], {cwd: repository, encoding: 'utf8'})
    .split('\0').filter(filename => /\.ya?ml$/.test(filename));
  for (const filename of yamlFiles) {
    const destination = path.join(infrastructure, filename);
    fs.mkdirSync(path.dirname(destination), {recursive: true});
    fs.copyFileSync(path.join(repository, filename), destination);
    let directory = path.dirname(destination);
    while (directory === infrastructure || directory.startsWith(infrastructure + path.sep)) {
      fs.writeFileSync(path.join(directory, '.env'), '');
      directory = path.dirname(directory);
    }
  }
  const infrastructureModel = JSON.parse(execFileSync('docker', ['compose', '--env-file', '/dev/null',
    '--project-name', 'doppler-fixture', '--project-directory', infrastructure,
    '-f', path.join(infrastructure, 'docker-compose.yml'),
    '-f', path.join(infrastructure, 'docker-compose.ports.yml'),
    '-f', path.join(infrastructure, 'credentials/doppler/docker-compose.yml'),
    'config', '--no-env-resolution', '--format', 'json'], {encoding: 'utf8'}));
  assert.equal(infrastructureModel.services['docker-webhook'].image, 'generaltao725/docker-webhook:0.10.0');
  assert.equal(infrastructureModel.services['docker-webhook'].build, undefined);
  assert.equal(infrastructureModel.services['docker-webhook'].environment.DOCKER_WEBHOOK_DOPPLER_MANAGED_DEPLOYMENTS, 'fullstack-starter-stage');
  assert(infrastructureModel.services['docker-webhook'].volumes.some(volume => volume.target === '/run/prodenv-doppler' && volume.read_only));
  assert(!infrastructureModel.services['docker-webhook'].volumes.some(volume => /tokens|prodenv-secrets/.test(volume.source)));
  assert(infrastructureModel.services.backrest.volumes.some(volume => volume.target === '/secret-recovery' && volume.read_only));
  console.log('Opt-in infrastructure overlay and token-free broker mounts passed');
} finally {
  fs.rmdirSync(temporary, {recursive: true});
}