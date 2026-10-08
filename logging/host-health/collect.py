import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
import time


PROFILE = "v1-read4k-100iops-sync4k-20iops"
PERIODS = {"cpu": 3600, "read": 10800, "sync": 86400}


def command(arguments, timeout=15, accepted=(0,)):
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env={**os.environ, "LC_ALL": "C"},
                          start_new_session=True) as process:
        try:
            output, error = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        if process.returncode not in accepted:
            raise RuntimeError(f"{arguments[0]} exited {process.returncode}")
        return output, error


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Metrics:
    def __init__(self):
        self.values = {}

    def add(self, metric, value, **labels):
        if not math.isfinite(value):
            raise ValueError("Non-finite measurement")
        self.values[(metric, tuple(sorted(labels.items())))] = value

    def render(self):
        lines = []
        described = set()
        for (name, labels), value in sorted(self.values.items()):
            if name not in described:
                metric_type = "counter" if name.endswith("_total") else "gauge"
                lines.append(f"# HELP prodenv_health_{name} Host {name.replace('_', ' ')}.\n")
                lines.append(f"# TYPE prodenv_health_{name} {metric_type}\n")
                described.add(name)
            suffix = "{" + ",".join(key + "=" + json.dumps(str(label))
                                     for key, label in labels) + "}" if labels else ""
            lines.append(f"prodenv_health_{name}{suffix} {value}\n")
        return "".join(lines)


def cpu_result(output):
    match = re.search(r"events per second:\s+([0-9.]+)", output)
    if not match or float(match[1]) <= 0:
        raise ValueError("Missing CPU result")
    return {"events_per_second": float(match[1])}


def fio_result(output, direction):
    jobs = json.loads(output)["jobs"]
    if len(jobs) != 1 or jobs[0]["error"] != 0:
        raise ValueError("Unsuccessful disk probe")
    job = jobs[0]
    operation = job["read" if direction == "read" else "write"]
    if operation["total_ios"] < 100:
        raise ValueError("Too few disk samples")
    values = {"iops": operation["iops"], "samples": operation["total_ios"]}
    latency = operation["clat_ns"]
    for percentile in (50, 95, 99):
        values[f"latency_p{percentile}_seconds"] = latency["percentile"][f"{percentile:.6f}"] / 1e9
    if direction == "sync":
        values["sync_p95_seconds"] = job["sync"]["lat_ns"]["percentile"]["95.000000"] / 1e9
    return values


def ping_result(output):
    loss = re.search(r"([0-9.]+)% packet loss", output)
    if not loss:
        raise ValueError("Missing packet statistics")
    values = {"loss_ratio": float(loss[1]) / 100}
    latency = re.search(r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", output)
    if latency:
        values.update(rtt_seconds=float(latency[2]) / 1000,
                      jitter_seconds=float(latency[4]) / 1000)
    return values


def pressure(path):
    content = path.read_text()
    return float(re.search(r"some avg10=([0-9.]+)", content)[1])


def headroom(proc=Path("/proc")):
    for process in proc.glob("[0-9]*/comm"):
        try:
            if process.read_text().strip() in ("restic", "pg_dump", "pg_restore", "sysbench", "fio"):
                return False
        except OSError:
            continue
    memory = dict(re.findall(r"^(\w+):\s+(\d+)", (proc / "meminfo").read_text(), re.M))
    if int(memory["MemAvailable"]) / int(memory["MemTotal"]) < 0.20:
        return False
    if os.getloadavg()[0] > (os.cpu_count() or 1) * 0.5:
        return False
    for resource in ("cpu", "io", "memory"):
        path = proc / "pressure" / resource
        if path.exists() and pressure(path) > 5:
            return False
    return True


def prepare_disk(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Disk probe requires a regular private file")
    if shutil.disk_usage(path.parent).free < 2 * 1024 ** 3:
        raise ValueError("Disk probe needs at least 2 GiB free")
    if not path.exists() or path.stat().st_size != 256 * 1024 ** 2:
        command(["fio", "--name=prepare", f"--filename={path}", "--size=256m",
                 "--rw=write", "--bs=1m", "--direct=1", "--rate=16m",
                 "--refill_buffers=1", "--end_fsync=1", "--output-format=json"], timeout=60)


def benchmark(name, directory):
    if name == "cpu":
        return cpu_result(command(["sysbench", "cpu", "--threads=1", "--time=15",
                                   "--cpu-max-prime=10000", "run"], timeout=25)[0])
    path = directory / "disk-probe.bin"
    prepare_disk(path)
    arguments = ["fio", f"--name={name}", f"--filename={path}", "--size=256m",
                 "--ioengine=psync", "--iodepth=1", "--numjobs=1", "--bs=4k",
                 "--runtime=30", "--time_based=1", "--output-format=json",
                 "--percentile_list=50:95:99", "--allow_file_create=0"]
    arguments += (["--rw=randread", "--direct=1", "--rate_iops=100", "--readonly"]
                  if name == "read" else ["--rw=write", "--direct=0", "--rate_iops=20", "--fdatasync=1"])
    return fio_result(command(arguments, timeout=45)[0], name)


def host_sample(proc=Path("/proc")):
    cpu = list(map(int, (proc / "stat").read_text().splitlines()[0].split()[1:9]))
    sectors = 0
    for line in (proc / "diskstats").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 14 and re.fullmatch(r"(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+)", fields[2]):
            sectors += int(fields[5]) + int(fields[9])
    return {"time": time.monotonic(), "cpu": cpu, "bytes": sectors * 512}


def sample_difference(before, after):
    ticks = [end - start for start, end in zip(before["cpu"], after["cpu"])]
    total = sum(ticks)
    if total <= 0 or min(ticks) < 0 or after["time"] <= before["time"]:
        return {}
    return {"cpu_busy_ratio": sum(ticks[index] for index in (0, 1, 2, 5, 6)) / total,
            "cpu_steal_ratio": ticks[7] / total,
            "disk_bytes_per_second": max(0, after["bytes"] - before["bytes"]) / (after["time"] - before["time"])}


def update_baseline(probe, now, values):
    samples = probe.setdefault("calibration", [])
    if "baseline" in probe:
        return
    samples.append({"time": now, "values": values})
    if now - samples[0]["time"] >= 7 * 86400 and len(samples) >= 7:
        probe["baseline"] = {key: statistics.median(sample["values"][key] for sample in samples)
                             for key in values}
        probe["calibration"] = []


def collect_probes(metrics, state, directory, now):
    versions = "|".join(command([tool, "--version"])[0].strip() for tool in ("fio", "sysbench"))
    profile = hashlib.sha256((PROFILE + versions).encode()).hexdigest()[:12]
    metrics.add("benchmark_info", 1, profile=profile, version=versions)
    if state.get("profile") != profile:
        state["profile"] = profile
        state["probes"] = {}
    probes = state.setdefault("probes", {})
    due = next((name for name, period in PERIODS.items()
                if now - probes.get(name, {}).get("attempt", 0) >= period), None)
    if due:
        probe = probes.setdefault(due, {})
        probe["attempt"] = now
        probe["status"] = 2
        started = time.monotonic()
        if headroom():
            try:
                before = host_sample()
                values = benchmark(due, directory)
                probe["context"] = sample_difference(before, host_sample())
                probe.update(status=1, success=now, values=values)
                probe["success_duration"] = time.monotonic() - started
                update_baseline(probe, now, values)
                key = "events_per_second" if due == "cpu" else "sync_p95_seconds" if due == "sync" else "latency_p95_seconds"
                baseline = probe.get("baseline", {}).get(key)
                degraded = baseline and (values[key] < baseline * 0.75 if due == "cpu"
                                          else values[key] > max(baseline * 2, 0.02 if due == "sync" else 0.01))
                probe["degraded_runs"] = probe.get("degraded_runs", 0) + 1 if degraded else 0
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
                probe["status"] = 0
        probe["duration"] = time.monotonic() - started
    for name, period in PERIODS.items():
        probe = probes.get(name, {})
        labels = {"probe": name, "profile": profile}
        metrics.add("probe_period_seconds", period, **labels)
        metrics.add("probe_status", probe.get("status", -1), **labels)
        metrics.add("probe_last_attempt_timestamp_seconds", probe.get("attempt", 0), **labels)
        metrics.add("probe_last_success_timestamp_seconds", probe.get("success", 0), **labels)
        metrics.add("probe_duration_seconds", probe.get("duration", 0), **labels)
        if "success_duration" in probe:
            metrics.add("probe_success_duration_seconds", probe["success_duration"], **labels)
        metrics.add("probe_baseline_ready", int("baseline" in probe), **labels)
        metrics.add("probe_degraded_runs", probe.get("degraded_runs", 0), **labels)
        for key, value in probe.get("values", {}).items():
            metrics.add("probe_" + key, value, **labels)
        for key, value in probe.get("baseline", {}).items():
            metrics.add("baseline_" + key, value, **labels)
        for key, value in probe.get("context", {}).items():
            metrics.add("probe_context_" + key, value, **labels)


def network_counters(metrics, proc=Path("/proc")):
    fields = {"receive_bytes_total": 0, "receive_packets_total": 1, "receive_errs_total": 2,
              "receive_drop_total": 3, "transmit_bytes_total": 8, "transmit_packets_total": 9,
              "transmit_errs_total": 10, "transmit_drop_total": 11}
    for line in (proc / "net/dev").read_text().splitlines()[2:]:
        device, counters = line.split(":", 1)
        device = device.strip()
        if re.match(r"^(lo$|veth|docker|br-)", device):
            continue
        values = list(map(int, counters.split()))
        for name, index in fields.items():
            metrics.add("interface_" + name, values[index], device=device)
    lines = (proc / "net/snmp").read_text().splitlines()
    for index in range(0, len(lines) - 1, 2):
        if lines[index].startswith("Tcp:"):
            counters = dict(zip(lines[index].split()[1:], lines[index + 1].split()[1:]))
            for name in ("OutSegs", "RetransSegs"):
                metrics.add("tcp_" + name + "_total", int(counters[name]))


def collect_network(metrics, config):
    network_counters(metrics)
    for target in config["ping_targets"]:
        try:
            output, _ = command(["ping", "-n", "-c", "5", "-i", "0.2", "-W", "1", "-w", "6", target],
                                timeout=8, accepted=(0, 1))
            for key, value in ping_result(output).items():
                metrics.add("network_" + key, value, target=target)
            metrics.add("network_probe_success", 1, target=target)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            metrics.add("network_probe_success", 0, target=target)
    started = time.monotonic()
    try:
        command(["getent", "ahostsv4", config["dns_target"]], timeout=5)
        metrics.add("dns_seconds", time.monotonic() - started)
        metrics.add("dns_success", 1)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        metrics.add("dns_success", 0)
    for target in config["http_targets"]:
        name, url = target["name"], target["url"]
        try:
            output, _ = command(["curl", "--silent", "--show-error", "--location", "--max-time", "8",
                                 "--output", "/dev/null", "--write-out", "%{json}", url], timeout=10)
            result = json.loads(output)
            metrics.add("http_success", int(200 <= result["http_code"] < 400), target=name, vantage="local")
            metrics.add("http_seconds", result["time_total"], target=name, vantage="local")
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
            metrics.add("http_success", 0, target=name, vantage="local")


def collect_system(metrics, state):
    output, _ = command(["systemctl", "--failed", "--type=service", "--no-legend", "--plain", "--no-pager"])
    failed = [line.split()[0] for line in output.splitlines() if line.strip()]
    metrics.add("failed_services", len(failed))
    for service in failed:
        metrics.add("service_failed", 1, service=service)
    output, _ = command(["docker", "ps", "--all", "--quiet"])
    identifiers = output.split()
    containers = json.loads(command(["docker", "inspect", *identifiers], timeout=20)[0]) if identifiers else []
    previous = state.setdefault("containers", {})
    current = {}
    for container in containers:
        if container["HostConfig"]["RestartPolicy"]["Name"] in ("", "no"):
            continue
        labels = container["Config"].get("Labels") or {}
        name = container["Name"].lstrip("/")
        scope = {"name": name, "project": labels.get("com.docker.compose.project", "infrastructure"),
                 "service": labels.get("com.docker.compose.service", name)}
        status = container["State"]
        old = previous.get(name, {})
        count = container["RestartCount"]
        changed = bool(old and old["started"] != status["StartedAt"])
        delta = max(0, count - old.get("count", count)) if old.get("id") == container["Id"] else 0
        restarts = old.get("restarts", 0) + max(int(changed), delta)
        current[name] = {"started": status["StartedAt"], "count": count, "id": container["Id"], "restarts": restarts}
        metrics.add("container_running", int(status["Running"]), **scope)
        metrics.add("container_restarts_total", restarts, **scope)
        metrics.add("container_oom_killed", int(status.get("OOMKilled", False)), **scope)
        if "Health" in status:
            metrics.add("container_healthy", int(status["Health"]["Status"] == "healthy"), **scope)
    state["containers"] = current


def collect_maintenance(metrics, config):
    checker = Path("/usr/lib/update-notifier/apt-check")
    metrics.add("security_supported", int(checker.is_file()))
    if checker.is_file():
        output, error = command([str(checker)], timeout=30)
        updates, security = map(int, (error.strip() or output.strip()).split(";"))
        metrics.add("updates_pending", updates)
        metrics.add("security_updates_pending", security)
    stamp = Path("/var/lib/apt/periodic/update-success-stamp")
    if stamp.is_file():
        metrics.add("package_index_timestamp_seconds", stamp.stat().st_mtime)
    metrics.add("reboot_required", int(Path("/run/reboot-required").exists()))
    for certificate in config["certificates"]:
        path = Path(certificate["path"])
        output, _ = command(["openssl", "x509", "-in", str(path), "-noout", "-enddate"])
        expiry = datetime.datetime.strptime(output.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")
        metrics.add("certificate_expiry_timestamp_seconds", expiry.replace(tzinfo=datetime.timezone.utc).timestamp(),
                    certificate=certificate["name"])


def collect_bandwidth(metrics, state, config, now):
    settings = config.get("bandwidth", {})
    period = settings.get("period_seconds", 604800)
    result = state.setdefault("bandwidth", {})
    if settings.get("url") and now - result.get("attempt", 0) >= period:
        result.update(attempt=now, status=2)
        if headroom():
            try:
                output, _ = command(["curl", "--silent", "--show-error", "--fail", "--max-time", "15",
                                     "--max-filesize", "10485760", "--output", "/dev/null", "--write-out", "%{json}",
                                     settings["url"]], timeout=20)
                data = json.loads(output)
                if data["size_download"] != 10485760:
                    raise ValueError("Incomplete bandwidth sample")
                result.update(status=1, success=now, speed=data["speed_download"])
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
                result["status"] = 0
    metrics.add("bandwidth_status", result.get("status", -1))
    metrics.add("bandwidth_last_success_timestamp_seconds", result.get("success", 0))
    if "speed" in result:
        metrics.add("bandwidth_bytes_per_second", result["speed"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/prodenv-health.json")
    parser.add_argument("--state-directory", default="/var/lib/prodenv-health")
    parser.add_argument("--metrics-directory", required=True)
    arguments = parser.parse_args()
    directory = Path(arguments.state_directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        config = json.loads(Path(arguments.config).read_text())
        state_path = directory / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        metrics = Metrics()
        now = time.time()
        collectors = {
            "network": lambda: collect_network(metrics, config),
            "system": lambda: collect_system(metrics, state),
            "maintenance": lambda: collect_maintenance(metrics, config),
            "benchmark": lambda: collect_probes(metrics, state, directory, now),
            "bandwidth": lambda: collect_bandwidth(metrics, state, config, now),
        }
        for name, collect in collectors.items():
            try:
                collect()
                metrics.add("collector_success", 1, collector=name)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
                metrics.add("collector_success", 0, collector=name)
                print(f"Collector failed: {name}", flush=True)
        metrics.add("last_run_timestamp_seconds", now)
        atomic_write(state_path, json.dumps(state))
        atomic_write(Path(arguments.metrics_directory) / "host-health.prom", metrics.render())


if __name__ == "__main__":
    main()