import csv
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timezone


SPEC = importlib.util.spec_from_file_location("retained_integration", Path(__file__).with_name("worker.py"))
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


@unittest.skipUnless(os.environ.get("RUN_BACKUP_INTEGRATION") == "1", "Enable PostgreSQL/restic integration checks explicitly")
class RetainedIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="retained-integration-")
        cls.root = Path(cls.temporary.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        password = cls.root / "password"
        password.write_text("isolated-test-password")
        cls.environment = mock.patch.dict(os.environ, {"PGHOST": "127.0.0.1", "PGPORT": port, "PGUSER": "postgres", "PGDATABASE": "postgres", "BACKREST_REPOSITORY_URI": str(cls.root / "repository"), "RESTIC_PASSWORD_FILE": str(password), "BACKREST_INSTANCE": "test-instance"})
        cls.environment.start()
        cls.config = json.loads(Path(__file__).with_name("config.json").read_text())
        cls.config.update(stagingRoot=str(cls.root / "staging"),coreRoot=str(cls.root / "core"),metricsDirectory=str(cls.root / "metrics"),stateDirectory=str(cls.root / "state"),allowedProjects=["fixture"],expectedProjects=["fixture"],commandTimeoutSeconds=120)
        subprocess.run(["initdb", "-D", str(cls.root / "db"), "-A", "trust", "-U", "postgres", "--locale=C", "--encoding=UTF8"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["pg_ctl", "-D", str(cls.root / "db"), "-l", str(cls.root / "postgres.log"), "-o", f"-F -p {port} -h 127.0.0.1 -k {cls.root}", "-w", "start"], check=True, stdout=subprocess.DEVNULL)
        WORKER.run(["psql", "-X", "-U", "postgres", "-c", "CREATE TABLE chat_message (id text PRIMARY KEY, message_text text, sent_at timestamptz)"], cls.config)
        WORKER.restic(["init"], cls.config)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["pg_ctl", "-D", str(cls.root / "db"), "-m", "immediate", "-w", "stop"], check=True, stdout=subprocess.DEVNULL)
        cls.environment.stop()
        cls.temporary.cleanup()

    def cohort(self, dataset, day, retention, created):
        directory = self.root / "staging/fixture/current" / dataset / day
        directory.mkdir(parents=True)
        start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        metadata = {"format": "postgres-csv-v1", "project": "fixture", "dataset": dataset, "recordDate": day, "createdAt": created, "expiresAt": start + retention, "retentionSeconds": retention}
        return directory, metadata

    def finish(self, directory, metadata):
        (directory / "metadata.json").write_text(json.dumps(metadata))
        (directory / "checksums.sha256").write_text("".join(f"{WORKER.digest(path)}  {path.name}\n" for path in sorted(directory.iterdir())))

    def test_postgres_cohort_survives_encrypted_remote_round_trip(self):
        now = int(time.time())
        day = datetime.now(timezone.utc).date().isoformat()
        directory, metadata = self.cohort("chat", day, 7 * 86400, now)
        WORKER.run(["pg_dump", "--format=custom", "--schema-only", "--file", str(directory / "database.dump")], self.config)
        with (directory / "chat_message.csv").open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["id", "message_text", "sent_at"])
            writer.writerow(["test", "comma, newline\nand quotes \"", datetime.now(timezone.utc).isoformat()])
        (directory / "restore.sql").write_text("\\copy public.chat_message FROM 'chat_message.csv' WITH (FORMAT CSV, HEADER TRUE)\n")
        self.finish(directory, metadata)
        snapshot = WORKER.backup_cohort(directory, metadata, [], self.config)
        with tempfile.TemporaryDirectory(dir=self.root) as destination:
            WORKER.restic(["restore", snapshot, "--target", destination], self.config)
            restored = Path(destination) / str(directory).lstrip("/")
            WORKER.restore_check(restored, json.loads((restored / "metadata.json").read_text()), self.config)
        project = self.root / "staging/fixture"
        feed = project / "policy.json"
        feed.write_text(json.dumps({"updatedAt": int(time.time()), "retentionSeconds": {"chat": 7 * 86400, "audit": 14 * 86400}}))
        core = self.root / "core/fixture"
        core.mkdir(parents=True)
        (core / ".last-success").write_text(str(int(time.time())))
        state = {}
        WORKER.cycle(self.config, state)
        self.assertEqual(state["fixture/chat"]["success"], 1)
        self.assertGreater(state["fixture/chat"]["validationTimestamp"], 0)
        feed.write_text(json.dumps({"updatedAt": 0, "retentionSeconds": {"chat": 7 * 86400, "audit": 14 * 86400}}))
        with mock.patch.object(WORKER, "backup_cohort") as upload:
            with self.assertRaisesRegex(RuntimeError, "policy feed is stale"):
                WORKER.cycle(self.config, state)
            upload.assert_not_called()


    def test_old_records_expire_even_when_the_snapshot_was_just_created(self):
        old = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
        directory, metadata = self.cohort("audit", "2020-01-01", 14 * 86400, old + 3600)
        (directory / "payload.csv").write_text("unique expired content")
        self.finish(directory, metadata)
        with mock.patch.object(WORKER.time, "time", return_value=old + 3600):
            snapshot = WORKER.backup_cohort(directory, metadata, [], self.config)
        self.assertEqual(WORKER.expire(self.config), 1)
        remaining = json.loads(WORKER.restic(["snapshots", "--json", "--tag", WORKER.MANAGED_TAG], self.config)) or []
        self.assertFalse(any(item["id"].startswith(snapshot) for item in remaining))
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
