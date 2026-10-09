const {execFileSync} = require('child_process');

function audit(container, hostMemory) {
  const limits = container.HostConfig || {};
  const labels = container.Config && container.Config.Labels || {};
  const cpu = Number(limits.NanoCpus) / 1e9 || (limits.CpuQuota > 0 && limits.CpuPeriod > 0 ? limits.CpuQuota / limits.CpuPeriod : 0);
  const issues = [];
  if (!(limits.Memory > 0)) issues.push('No RAM limit');
  else if (limits.Memory >= hostMemory) issues.push('RAM limit is not below host capacity');
  if (!(limits.MemorySwap >= limits.Memory && limits.MemorySwap > 0)) issues.push('No bounded RAM + swap limit');
  if (!(cpu > 0)) issues.push('No CPU quota');
  if (!(limits.PidsLimit > 0)) issues.push('No PID limit');
  return {
    container: String(container.Name || container.Id).replace(/^\//, ''),
    project: labels['com.docker.compose.project'] || '(not Compose)',
    service: labels['com.docker.compose.service'] || '',
    replica: labels['com.docker.compose.container-number'] || '',
    composeFiles: labels['com.docker.compose.project.config_files'] || '',
    memoryMiB: Number(limits.Memory || 0) / 1048576,
    memoryAndSwapMiB: Number(limits.MemorySwap || 0) / 1048576,
    cpus: cpu, pids: limits.PidsLimit, issues,
  };
}

module.exports = {audit};

if (require.main === module) {
  const docker = args => execFileSync('docker', args, {encoding: 'utf8'}).trim();
  const ids = docker(['ps', '-q']).split(/\s+/).filter(Boolean);
  if (!ids.length) {
    console.error('No running containers to audit.');
    process.exitCode = 1;
  } else {
    const hostMemory = Number(docker(['info', '--format', '{{.MemTotal}}']));
    if (!(hostMemory > 0)) throw new Error('Docker did not return host memory capacity.');
    const results = ids.map(id => audit(JSON.parse(docker(['inspect', id]))[0], hostMemory));
    const failures = results.filter(result => result.issues.length);
    console.log(JSON.stringify({checked: results.length, bounded: results.length - failures.length, failures}, null, 2));
    if (failures.length) process.exitCode = 1;
  }
}