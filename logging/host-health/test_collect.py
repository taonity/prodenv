import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collect


class CollectorTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("HOST_HEALTH_SMOKE") == "1", "Explicit disposable smoke test only")
    def test_real_tools_on_temporary_files(self):
        self.assertGreater(collect.cpu_result(collect.command([
            "sysbench", "cpu", "--threads=1", "--time=1", "run"])[0])["events_per_second"], 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.bin"
            collect.command(["fio", "--name=prepare", f"--filename={path}", "--size=4m", "--rw=write", "--bs=1m", "--end_fsync=1"])
            for name, options in (("read", ["--rw=randread", "--direct=1", "--readonly"]),
                                  ("sync", ["--rw=write", "--direct=0", "--fdatasync=1"])):
                output, _ = collect.command(["fio", f"--name={name}", f"--filename={path}", "--size=4m",
                                             "--ioengine=psync", "--bs=4k", "--runtime=2", "--time_based=1",
                                             "--rate_iops=100", "--output-format=json", "--percentile_list=50:95:99",
                                             "--allow_file_create=0", *options])
                result = collect.fio_result(output, name)
                self.assertGreater(result["samples"], 100)
                self.assertGreater(result["latency_p95_seconds"], 0)

    def test_cpu_result(self):
        self.assertEqual(collect.cpu_result("CPU speed:\n events per second: 123.45"), {"events_per_second": 123.45})
        for output in ("", "events per second: 0"):
            with self.assertRaises(ValueError):
                collect.cpu_result(output)

    def test_disk_result_and_failed_jobs(self):
        percentiles = {f"{number:.6f}": number * 1000000 for number in (50, 95, 99)}
        operation = {"iops": 99, "total_ios": 3000, "clat_ns": {"percentile": percentiles}}
        job = {"error": 0, "read": operation, "write": operation, "sync": {"lat_ns": {"percentile": percentiles}}}
        result = collect.fio_result(json.dumps({"jobs": [job]}), "sync")
        self.assertEqual(result["sync_p95_seconds"], 0.095)
        self.assertEqual(result["latency_p99_seconds"], 0.099)
        job["error"] = 5
        with self.assertRaises(ValueError):
            collect.fio_result(json.dumps({"jobs": [job]}), "read")

    def test_ping_loss_is_not_zero_latency(self):
        self.assertEqual(collect.ping_result("5 packets transmitted, 0 received, 100% packet loss"), {"loss_ratio": 1})
        result = collect.ping_result("0% packet loss\nrtt min/avg/max/mdev = 1.0/2.0/4.0/0.5 ms")
        self.assertEqual(result["rtt_seconds"], 0.002)
        self.assertEqual(result["jitter_seconds"], 0.0005)

    def test_fixed_baseline(self):
        probe = {}
        for day in range(8):
            collect.update_baseline(probe, day * 86400, {"events_per_second": 100 + day})
        self.assertEqual(probe["baseline"]["events_per_second"], 103.5)
        collect.update_baseline(probe, 20 * 86400, {"events_per_second": 1})
        self.assertEqual(probe["baseline"]["events_per_second"], 103.5)

    def test_metrics_escaping_and_atomic_write(self):
        metrics = collect.Metrics()
        metrics.add("sample", 1, target='a"b\\c\n')
        self.assertIn('target="a\\"b\\\\c\\n"', metrics.render())
        with self.assertRaises(ValueError):
            metrics.add("sample", float("nan"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.prom"
            collect.atomic_write(path, metrics.render())
            self.assertEqual(path.read_text(), metrics.render())
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    @patch.object(collect, "command", return_value=("version1", ""))
    @patch.object(collect, "headroom", return_value=False)
    def test_busy_probe_is_skipped_and_not_successful(self, *_):
        metrics, state = collect.Metrics(), {}
        collect.collect_probes(metrics, state, Path("/unused"), 1000000)
        self.assertEqual(state["probes"]["cpu"]["status"], 2)
        self.assertNotIn("success", state["probes"]["cpu"])
        self.assertIn('probe_last_success_timestamp_seconds{probe="cpu"', metrics.render())

    @patch.object(collect, "command", return_value=("version1", ""))
    @patch.object(collect, "headroom", return_value=True)
    @patch.object(collect, "benchmark", side_effect=ValueError("failed"))
    def test_failed_probe_preserves_but_does_not_refresh_success(self, *_):
        profile = collect.hashlib.sha256((collect.PROFILE + "version1|version1").encode()).hexdigest()[:12]
        state = {"profile": profile, "probes": {"cpu": {"success": 1, "values": {"events_per_second": 100}}}}
        collect.collect_probes(collect.Metrics(), state, Path("/unused"), 1000000)
        self.assertEqual(state["probes"]["cpu"]["status"], 0)
        self.assertEqual(state["probes"]["cpu"]["success"], 1)

    def test_disk_target_must_be_regular(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                collect.prepare_disk(Path(directory))

    def test_backups_prevent_benchmarking(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "100").mkdir()
            (proc / "100/comm").write_text("pg_dump\n")
            self.assertFalse(collect.headroom(proc))

    def test_container_collection_exports_names_and_restart_changes(self):
        container = {"Id": "abc", "Name": "/project-prod-db-1", "RestartCount": 2,
                     "Config": {"Labels": {"com.docker.compose.project": "project-prod", "com.docker.compose.service": "db"}},
                     "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                     "State": {"Running": True, "StartedAt": "today", "OOMKilled": False}}
        state = {"containers": {"project-prod-db-1": {"id": "abc", "started": "yesterday", "count": 0, "restarts": 0}}}
        metrics = collect.Metrics()
        with patch.object(collect, "command", side_effect=[("", ""), ("abc\n", ""), (json.dumps([container]), "")]):
            collect.collect_system(metrics, state)
        self.assertIn('name="project-prod-db-1"', metrics.render())
        self.assertEqual(state["containers"]["project-prod-db-1"]["restarts"], 2)
        self.assertNotIn('container_healthy', metrics.render())

    def test_context_excludes_steal_and_iowait_from_busy(self):
        before = {"time": 1, "cpu": [0] * 8, "bytes": 0}
        after = {"time": 11, "cpu": [10, 0, 10, 50, 10, 0, 0, 20], "bytes": 10240}
        self.assertEqual(collect.sample_difference(before, after), {
            "cpu_busy_ratio": 0.2, "cpu_steal_ratio": 0.2, "disk_bytes_per_second": 1024})

    def test_security_listener_scope_and_bounded_firewall_labels(self):
        listeners = "tcp LISTEN 0 128 127.0.0.1:9000 0.0.0.0:*\ntcp LISTEN 0 128 [::]:443 [::]:*\n"
        messages = ["[UFW BLOCK] SRC=198.51.100.1 PROTO=TCP DPT=443",
                    "[UFW REJECT] SRC=198.51.100.1 PROTO=UDP DPT=54321"]
        journal = "\n".join(json.dumps({"MESSAGE": message}) for message in messages)
        metrics = collect.Metrics()
        with patch.object(collect, "command", side_effect=[(listeners, ""), (journal, "")]):
            collect.collect_security(metrics, {})
        rendered = metrics.render()
        self.assertIn('binding="loopback",port="9000"', rendered)
        self.assertIn('binding="non_loopback",port="443"', rendered)
        self.assertIn('firewall_logged_drops_5m{port="other",protocol="UDP"} 1', rendered)
        self.assertIn('firewall_source_count_5m 1', rendered)
        self.assertNotIn("198.51.100.1", rendered)

    def test_no_firewall_records_are_unknown_not_zero_drops(self):
        metrics = collect.Metrics()
        with patch.object(collect, "command", return_value=("", "")):
            collect.collect_security(metrics, {})
        self.assertIn("firewall_observation_available 0", metrics.render())
        self.assertNotIn("firewall_logged_drops", metrics.render())

    def test_firewall_sample_is_bounded_and_marked(self):
        record = json.dumps({"MESSAGE": "[UFW BLOCK] SRC=198.51.100.1 PROTO=TCP DPT=80"})
        metrics = collect.Metrics()
        with patch.object(collect, "command", side_effect=[("", ""), ((record + "\n") * 5001, "")]):
            collect.collect_security(metrics, {})
        self.assertIn('firewall_logged_drops_5m{port="80",protocol="TCP"} 5000', metrics.render())
        self.assertIn("firewall_sample_truncated 1", metrics.render())

    def test_published_ports_include_containers_without_restart_policy(self):
        container = {"Name": "/temporary", "HostConfig": {"RestartPolicy": {"Name": "no"}},
                     "NetworkSettings": {"Ports": {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3000"}]}}}
        metrics = collect.Metrics()
        with patch.object(collect, "command", side_effect=[("", ""), ("abc", ""), (json.dumps([container]), "")]):
            collect.collect_system(metrics, {})
        self.assertIn('container_published_port{address="0.0.0.0",binding="non_loopback",name="temporary",port="3000",target="3000/tcp"} 1', metrics.render())

    def test_host_network_counters_exclude_container_interfaces(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "net").mkdir()
            values = " ".join(str(index) for index in range(16))
            (proc / "net/dev").write_text("header\nheader\n" + "\n".join(f"{device}: {values}" for device in ("eth0", "lo", "veth123", "br-abcd")))
            (proc / "net/snmp").write_text("Tcp: OutSegs RetransSegs\nTcp: 1000 3\n")
            metrics = collect.Metrics()
            collect.network_counters(metrics, proc)
            self.assertIn('interface_transmit_bytes_total{device="eth0"} 8', metrics.render())
            self.assertNotIn('device="lo"', metrics.render())
            self.assertIn('tcp_RetransSegs_total 3', metrics.render())

    @patch.object(collect, "command", return_value=("version1", ""))
    @patch.object(collect, "headroom", return_value=True)
    @patch.object(collect, "host_sample", return_value={"time": 1, "cpu": [0] * 8, "bytes": 0})
    @patch.object(collect, "benchmark", return_value={"events_per_second": 60})
    def test_degradation_counts_runs_not_scrapes(self, *_):
        profile = collect.hashlib.sha256((collect.PROFILE + "version1|version1").encode()).hexdigest()[:12]
        state = {"profile": profile, "probes": {"cpu": {"baseline": {"events_per_second": 100}}}}
        collect.collect_probes(collect.Metrics(), state, Path("/unused"), 1000000)
        self.assertEqual(state["probes"]["cpu"]["degraded_runs"], 1)
        state["probes"]["read"] = {"attempt": 1000000}
        state["probes"]["sync"] = {"attempt": 1000000}
        collect.collect_probes(collect.Metrics(), state, Path("/unused"), 1000001)
        self.assertEqual(state["probes"]["cpu"]["degraded_runs"], 1)


if __name__ == "__main__":
    unittest.main()