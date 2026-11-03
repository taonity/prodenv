import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location("retained_worker", Path(__file__).with_name("worker.py"))
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


class RetainedWorkerTest(unittest.TestCase):
    def test_cleanup_keeps_last_confirmed_policy_when_feed_is_stale(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "bot"
            directory.mkdir()
            path = directory / "policy.json"
            path.write_text(json.dumps({"updatedAt": 100, "retentionSeconds": {"chat": 7 * 86400, "audit": 14 * 86400}}))
            config = {"stagingRoot": root, "allowedProjects": ["bot"]}
            with mock.patch.object(WORKER.time, "time", return_value=1000):
                self.assertEqual(WORKER.current_policies(config)[("bot", "chat")], 7 * 86400)
            with mock.patch.object(WORKER.time, "time", return_value=200):
                self.assertEqual(WORKER.current_policies(config)[("bot", "audit")], 14 * 86400)

    def test_expiry_uses_record_age_even_after_recent_backup(self):
        start = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
        snapshot = {"time": "2026-11-20T00:00:00Z", "tags": ["project:bot", "dataset:chat", "record-date:2026-11-01", f"expires-at:{start + 7 * 86400}"]}
        config = {"expirySafetySeconds": 3600}
        self.assertEqual(WORKER.expiry(snapshot, config), start + 7 * 86400 - 3600)
        self.assertEqual(WORKER.expiry(snapshot, config, {("bot", "chat"): 86400}), start + 86400 - 3600)

    def test_expiry_forgets_and_prunes_without_any_new_backup(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"stagingRoot": root, "stateDirectory": str(Path(root) / "state"), "allowedProjects": [], "expirySafetySeconds": 3600}
            snapshot = {"id": "old-id", "tags": ["project:bot", "dataset:chat", "record-date:2020-01-01", "expires-at:10"]}
            with mock.patch.object(WORKER, "restic", side_effect=[json.dumps([snapshot]), "", ""]) as restic:
                self.assertEqual(WORKER.expire(config), 1)
            self.assertEqual(restic.call_args_list[1].args[0], ["forget", "old-id"])
            self.assertEqual(restic.call_args_list[2].args[0], ["prune", "--max-unused", "0"])

    def test_orphaned_exports_and_expired_local_cohorts_are_removed(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "bot"
            cohort = project / "current/chat/2020-01-01"
            cohort.mkdir(parents=True)
            (cohort / "metadata.json").write_text(json.dumps({"dataset": "chat", "recordDate": "2020-01-01", "expiresAt": 10}))
            stale = project / ".export-crashed"
            stale.mkdir()
            (stale / "sensitive.csv").write_text("old message")
            (project / ".backup.lock").touch()
            config = {"stagingRoot": root, "stateDirectory": str(Path(root) / "state"), "allowedProjects": [], "expirySafetySeconds": 3600}
            with mock.patch.object(WORKER, "restic", side_effect=["[]", ""]):
                WORKER.expire(config)
            self.assertFalse(cohort.exists())
            self.assertFalse(stale.exists())

    def test_idle_expiration_does_not_prune_or_connect_to_postgres(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            (state / "restore").mkdir(parents=True)
            config = {"stagingRoot": root, "stateDirectory": str(state), "allowedProjects": [], "expirySafetySeconds": 3600}
            with mock.patch.object(WORKER, "restic", return_value="[]") as restic, mock.patch.object(WORKER, "run") as run:
                WORKER.expire(config)
            restic.assert_called_once_with(["snapshots", "--json", "--tag", WORKER.MANAGED_TAG], config)
            run.assert_not_called()

    def test_failed_prune_is_retried_after_snapshot_has_been_forgotten(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            config = {"stagingRoot": root, "stateDirectory": str(state), "allowedProjects": [], "expirySafetySeconds": 3600}
            snapshot = {"id": "expired", "tags": ["project:bot", "dataset:chat", "record-date:2020-01-01", "expires-at:10"]}
            responses = [json.dumps([snapshot]), "", subprocess.CalledProcessError(1, ["restic", "prune"]), "[]", "", "[]"]
            with mock.patch.object(WORKER, "restic", side_effect=responses) as restic:
                with self.assertRaises(subprocess.CalledProcessError):
                    WORKER.expire(config)
                self.assertTrue((state / "prune.pending").exists())
                WORKER.expire(config)
                self.assertFalse((state / "prune.pending").exists())
                WORKER.expire(config)
            commands = [call.args[0][0] for call in restic.call_args_list]
            self.assertEqual(commands, ["snapshots", "forget", "prune", "snapshots", "prune", "snapshots"])

    def test_upgrade_prunes_preexisting_orphaned_data_once(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            state.mkdir()
            (state / "expiry.json").write_text("{}")
            config = {"stagingRoot": root, "stateDirectory": str(state), "allowedProjects": [], "expirySafetySeconds": 3600}
            with mock.patch.object(WORKER, "restic", side_effect=["[]", "", "[]"]) as restic:
                WORKER.expire(config)
                WORKER.expire(config)
            self.assertEqual([call.args[0][0] for call in restic.call_args_list], ["snapshots", "prune", "snapshots"])

    def test_unchanged_remote_copy_skips_reading_local_payload(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            manifest = directory / "checksums.sha256"
            manifest.write_text("already verified at upload")
            now = datetime(2026, 11, 4, 4, tzinfo=timezone.utc).timestamp()
            start = datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()
            metadata = {"project": "bot", "dataset": "chat", "recordDate": "2026-11-04", "createdAt": now, "expiresAt": start + 7 * 86400, "retentionSeconds": 7 * 86400}
            snapshot = {"id": "existing", "tags": ["project:bot", "dataset:chat", "record-date:2026-11-04", f"checksum:{WORKER.digest(manifest)}"]}
            config = {"expirySafetySeconds": 3600, "maxExportAgeSeconds": 93600}
            with mock.patch.object(WORKER.time, "time", return_value=now), mock.patch.object(WORKER, "verify") as verify, mock.patch.object(WORKER, "restic") as restic:
                self.assertEqual(WORKER.backup_cohort(directory, metadata, [snapshot], config), "existing")
            verify.assert_not_called()
            restic.assert_not_called()

    def test_daily_schedule_catches_up_and_survives_restarts(self):
        config = {"backupScheduleTimeUtc": "02:00"}
        cases = [
            (datetime(2026, 11, 4, 1, tzinfo=timezone.utc), {}, 3600),
            (datetime(2026, 11, 4, 3, tzinfo=timezone.utc), {}, 0),
            (datetime(2026, 11, 4, 3, tzinfo=timezone.utc), {"lastScheduledDate": "2026-11-04"}, 23 * 3600),
            (datetime(2026, 11, 5, 3, tzinfo=timezone.utc), {"lastScheduledDate": "2026-11-04"}, 0),
        ]
        for now, state, delay in cases:
            with self.subTest(now=now, state=state):
                self.assertEqual(WORKER.backup_delay(config, state, now), delay)

    def test_daily_restore_check_is_not_skipped_when_previous_job_finished_later(self):
        config = {"validationIntervalSeconds": 86400}
        now = datetime(2026, 11, 4, 2, tzinfo=timezone.utc).timestamp()
        yesterday = datetime(2026, 11, 3, 2, 30, tzinfo=timezone.utc).timestamp()
        today = datetime(2026, 11, 4, 1, 30, tzinfo=timezone.utc).timestamp()
        self.assertTrue(WORKER.validation_due(config, {"validationTimestamp": yesterday}, now))
        self.assertFalse(WORKER.validation_due(config, {"validationTimestamp": today}, now))


    def test_checksum_verification_rejects_unlisted_payload_and_tampering(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            metadata = directory / "metadata.json"
            metadata.write_text("{}")
            (directory / "checksums.sha256").write_text(f"{WORKER.digest(metadata)}  metadata.json\n")
            WORKER.verify(directory)
            metadata.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                WORKER.verify(directory)
            metadata.write_text("{}")
            (directory / "unlisted.csv").write_text("leaked")
            with self.assertRaisesRegex(ValueError, "Incomplete checksum"):
                WORKER.verify(directory)

    def test_restore_refuses_expired_records_before_import(self):
        with mock.patch.object(WORKER, "verify"), mock.patch.object(WORKER, "run") as run:
            with self.assertRaisesRegex(ValueError, "expired cohort"):
                WORKER.restore_check(Path("unused"), {"expiresAt": 10}, {"expirySafetySeconds": 3600})
            run.assert_not_called()

    def test_missing_expected_data_has_visible_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"metricsDirectory": root, "expectedProjects": ["bot"], "allowedProjects": ["bot"]}
            WORKER.write_metrics(config, {"monitoring/alerts": {"success": 0}})
            output = (Path(root) / "retained-backup.prom").read_text()
            for dataset in ("core", "chat", "audit"):
                self.assertIn(f'project="bot",dataset="{dataset}"', output)
            self.assertIn('export_timestamp_seconds{project="bot",dataset="chat"} 0', output)
            self.assertNotIn('project="monitoring"', output)


if __name__ == "__main__":
    unittest.main()
