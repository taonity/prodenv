import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("restore_validator.py")
SPEC = importlib.util.spec_from_file_location("restore_validator", MODULE_PATH)
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class FakeClient:
    def latest_snapshot(self, repository, plan):
        return {"id": "latest"}

    def list_files(self, repository, snapshot, path):
        return [
            {"name": "database.dump", "size": "1024"},
            {"name": "checksums.sha256", "size": "80"},
        ]

    def restore(self, repository, plan, snapshot, path, target):
        restore_directory = Path(target)
        restore_directory.mkdir()
        dump = b"test database dump"
        (restore_directory / "database.dump").write_bytes(dump)
        (restore_directory / "checksums.sha256").write_text(
            f"{hashlib.sha256(dump).hexdigest()}  database.dump\n", encoding="utf-8"
        )
        return 1

    def wait_for_operation(self, operation_id, timeout_seconds):
        pass


class RestoreValidatorTest(unittest.TestCase):
    def test_dump_size_requires_dump_and_checksum(self):
        entries = [
            {"name": "database.dump", "size": "42"},
            {"name": "checksums.sha256", "size": "80"},
        ]
        self.assertEqual(VALIDATOR.dump_size(entries, "/userdata/project/postgres"), 42)
        with self.assertRaises(VALIDATOR.ValidationError):
            VALIDATOR.dump_size(entries[:1], "/userdata/project/postgres")

    def test_guardrails_reject_oversized_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = {"maxDumpBytes": 100, "minimumFreeBytes": 1}
            with self.assertRaisesRegex(VALIDATOR.ValidationError, "allowed range"):
                VALIDATOR.check_guardrails(entry, 101, Path(directory))

    def test_validate_entry_leaves_target_creation_to_backrest(self):
        config = {"repository": "offsite", "plan": "all-project-exports"}
        entry = {
            "project": "fullstack-starter-prod",
            "path": "/userdata/fullstack-starter-prod/postgres",
            "maxDumpBytes": 2048,
            "minimumFreeBytes": 1,
            "restoreTimeoutSeconds": 60,
            "importTimeoutSeconds": 60,
        }
        with tempfile.TemporaryDirectory() as directory:
            restore_root = Path(directory)
            target = restore_root / entry["project"]
            target.mkdir()
            (target / "abandoned.dump").touch()
            with mock.patch.object(VALIDATOR, "validate_database") as validate:
                VALIDATOR.validate_entry(FakeClient(), config, entry, restore_root)
                validate.assert_called_once_with(target / "database.dump", 60)
            self.assertFalse(target.exists())

    def test_run_once_records_stable_project_metrics(self):
        config = {
            "repository": "offsite",
            "plan": "all-project-exports",
            "intervalDays": 90,
            "entries": [
                {
                    "project": "fullstack-starter-prod",
                    "path": "/userdata/fullstack-starter-prod/postgres",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(VALIDATOR, "validate_entry") as validate:
                VALIDATOR.run_once(FakeClient(), config, root, root, now=1234)
                validate.assert_called_once()
            metrics = (root / "restore-validation.prom").read_text()
            self.assertIn('project="fullstack-starter-prod"', metrics)
            self.assertIn("last_attempt_success", metrics)
            self.assertIn("last_success_timestamp_seconds", metrics)
            with mock.patch.object(VALIDATOR, "validate_entry") as validate:
                VALIDATOR.run_once(FakeClient(), config, root, root, now=1235)
                validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()