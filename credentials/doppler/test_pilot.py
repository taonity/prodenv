import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import pilot
import broker


def fixture_config():
    return {
        "deployment": "fullstack-starter-stage",
        "enabled": True, "state_directory": "/fixture/state",
        "checkout": "/fixture/starter", "host_override": "/fixture/override.yml",
        "metrics_directory": "/fixture/metrics", "doppler_executable": "/fixture/doppler", "age_executable": "/fixture/age",
        "recovery": {"directory": "/fixture/recovery", "recipients": ["age1fixture"], "max_age_hours": 48, "keep_exports": 4},
        "roles": {"admin": "postgres", "app": "starter_app", "migration": "starter_migration", "backup": "starter_backup"},
         "identities": {"sandbox": {"token_file": "/etc/prodenv-doppler/tokens/sandbox",
                        "project": "sandbox", "config": "stg", "expires_at": "never"}},
         "credentials": {name: {"source": "sandbox", "key": "FULLSTACK_STARTER_STAGE_" + name.upper(),
                       "expires_at": "never", "review_at": "unknown"}
                   for name in pilot.CREDENTIAL_IDENTITIES},
    }


def fixture_values():
    return {name: name + "_fixture:'\\$ !" for name in pilot.CREDENTIAL_IDENTITIES}


class PilotTest(unittest.TestCase):
    def test_unchanged_successful_poll_clears_previous_delivery_failure(self):
        config = fixture_config()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "pilot.json"
            manifest.write_text(json.dumps(config))
            with patch.object(pilot.sys, "argv", ["pilot.py", "poll", str(manifest)]), \
                    patch.object(Path, "lstat", return_value=Mock(st_mode=0o100600, st_uid=0)), \
                    patch.object(pilot.os, "umask"), \
                    patch.object(pilot, "write_metrics"), \
                    patch.object(pilot, "deploy", return_value=False), \
                    patch.object(pilot, "record_delivery") as recorded:
                self.assertEqual(pilot.main(), 0)
                recorded.assert_called_once_with(config, True)

    def test_catalog_preserves_free_plan_headroom_and_approved_boundaries(self):
        catalog = json.loads(Path(__file__).with_name("catalog.json").read_text())
        projects = catalog["projects"]
        tokens = sum(len(project["configs"]) for project in projects.values())
        configs = sum(len(project["configs"]) + int(not project["root_contains_secrets"]) for project in projects.values())
        self.assertEqual((len(projects), tokens, configs), (6, 22, 27))
        self.assertLessEqual(tokens * 2, catalog["limits"]["service_tokens"])
        self.assertEqual(catalog["sandbox_deployments"], ["fullstack-starter-stage", "fullstack-starter-prod", "sinair-llm-bot-stage", "artist-insight-service-stage", "gentool-data-viewer-stage"])
        for name, project in projects.items():
            self.assertEqual(len(project["configs"]), len(set(project["configs"])))
            self.assertLessEqual(len(project["configs"]) + int(not project["root_contains_secrets"]), catalog["limits"]["configs_per_environment"])
            if name != "sandbox":
                self.assertFalse(project["root_contains_secrets"])
                self.assertNotIn(project["environment"], project["configs"])
        self.assertEqual((catalog["config_syncs"], catalog["webhooks"], catalog["native_reminders"]), (0, 0, 0))

    def test_recovery_backup_validator_and_offsite_marker(self):
        scripts = Path(__file__).resolve().parents[2] / "backup/scripts"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = ["sh", str(scripts / "validate-secret-recovery.sh"), str(root)]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            (root / "snapshot.age").write_bytes(b"age-encryption.org/v1\nfixture")
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            (root / "unexpected.json").write_text("must-not-back-up")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            (root / "unexpected.json").unlink()
            with tempfile.TemporaryDirectory() as metrics:
                result = subprocess.run(["sh", str(scripts / "record-backup-metric.sh"), "secret-recovery"],
                                        env={"PATH": os.defpath, "METRICS_DIRECTORY": metrics}, capture_output=True)
                self.assertEqual(result.returncode, 0)
                self.assertIn('operation="secret-recovery"', (Path(metrics) / "backup-secret-recovery.prom").read_text())
                (root / "plaintext.age").write_text("not encrypted\n")
                self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
                (root / "plaintext.age").unlink()

    def test_polling_only_changes_app_secrets_and_requires_confirmed_db_maintenance(self):
        config, values = fixture_config(), fixture_values()
        previous = pilot.deployment_state(config, values)
        self.assertFalse(pilot.check_update(previous, previous))
        values["google_client_secret"] = "changed-fixture"
        self.assertTrue(pilot.check_update(previous, pilot.deployment_state(config, values)))
        values["db_app_password"] = "changed-database-fixture"
        with self.assertRaises(pilot.PilotError):
            pilot.check_update(previous, pilot.deployment_state(config, values))
        self.assertTrue(pilot.check_update(previous, pilot.deployment_state(config, values), bootstrap=True))
        for value in values.values():
            self.assertNotIn(value, json.dumps(pilot.deployment_state(config, values)))

    def test_broker_restricts_deployments_and_never_returns_process_output(self):
        calls = []
        def runner(arguments, **options):
            calls.append(arguments)
            self.assertEqual(options["stdout"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            return subprocess.CompletedProcess(arguments, 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fullstack-starter-stage.json").write_text("{}")
            self.assertEqual(broker.dispatch("../../etc/shadow", root, runner), 403)
            self.assertEqual(broker.dispatch("sinair-llm-bot-prod", root, runner), 403)
            self.assertEqual(broker.dispatch("fullstack-starter-prod", root, runner), 404)
            self.assertEqual(broker.dispatch("fullstack-starter-stage", root, runner), 200)
        self.assertEqual(len(calls), 1)

    def test_config_rejects_wrong_namespace_and_reused_keys(self):
        config = fixture_config()
        pilot.validate_config(config)
        config["credentials"]["db_admin_password"]["key"] = "SINAIR_LLM_BOT_PROD_PASSWORD"
        with self.assertRaises(pilot.PilotError):
            pilot.validate_config(config)
        config["credentials"]["db_admin_password"]["key"] = config["credentials"]["db_app_password"]["key"]
        with self.assertRaises(pilot.PilotError):
            pilot.validate_config(config)

    def test_distinct_roles_and_protected_sandbox_scope_required(self):
        for change in (lambda config: config["roles"].update(app="postgres"),
                       lambda config: config["identities"]["sandbox"].update(config="prd"),
                       lambda config: config["identities"]["sandbox"].update(token_file="/workspace/token")):
            config = fixture_config()
            change(config)
            with self.assertRaises(pilot.PilotError):
                pilot.validate_config(config)

    def test_fetch_is_scoped_and_never_puts_token_in_arguments(self):
        config = fixture_config()
        calls = []
        values = fixture_values()

        def run(arguments, environment):
            calls.append(arguments)
            self.assertNotIn("dp.st.fixture-token", arguments)
            self.assertEqual(environment["DOPPLER_TOKEN"], "dp.st.fixture-token")
            self.assertEqual(set(environment), {"PATH", "HOME", "XDG_CONFIG_HOME", "DOPPLER_TOKEN"})
            self.assertIn("--no-fallback", arguments)
            self.assertIn("--no-file", arguments)
            return json.dumps({"DOPPLER_PROJECT": "sandbox", "DOPPLER_CONFIG": "stg", "DOPPLER_ENVIRONMENT": "stg",
                               **{spec["key"]: values[alias] for alias, spec in config["credentials"].items()}})

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(pilot.fetch_credentials(config, directory, run, lambda filename: "dp.st.fixture-token"), values)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(len(calls), 1)

    def test_wrong_project_and_multiline_values_are_rejected(self):
        config = fixture_config()
        for value, project in (("safe", "wrong"), ("bad\nvalue", "sandbox")):
            def run(arguments, environment):
                return json.dumps({"DOPPLER_PROJECT": project, "DOPPLER_CONFIG": "stg", "DOPPLER_ENVIRONMENT": "stg",
                                   **{spec["key"]: value for spec in config["credentials"].values()}})
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(pilot.PilotError):
                pilot.fetch_credentials(config, directory, run, lambda filename: "dp.st.fixture-token")

    def test_sandbox_rejects_production_and_global_secrets(self):
        for key in ("SINAIR_LLM_BOT_PROD_PASSWORD", "CLOUDFLARE_API_TOKEN", "UNSCOPED_PASSWORD"):
            def run(arguments, environment):
                return json.dumps({"DOPPLER_PROJECT": "sandbox", "DOPPLER_CONFIG": "stg", "DOPPLER_ENVIRONMENT": "stg", key: "fixture"})
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(pilot.PilotError):
                pilot.fetch_credentials(fixture_config(), directory, run, lambda filename: "dp.st.fixture-token")

    def test_process_failure_output_is_not_disclosed(self):
        result = subprocess.CompletedProcess([], 1, "sensitive-stdout", "sensitive-stderr")
        with patch.object(pilot.subprocess, "run", return_value=result):
            with self.assertRaises(pilot.PilotError) as caught:
                pilot.quiet_run(["doppler", "secrets", "download", "--no-file"])
        self.assertNotIn("sensitive", str(caught.exception))

    def test_token_permissions_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            path = Path(directory) / "token"
            path.write_text("fixture-token")
            path.chmod(0o600)
            self.assertEqual(pilot.protected_token(path), "fixture-token")
            path.chmod(0o644)
            with self.assertRaises(pilot.PilotError):
                pilot.protected_token(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                pilot.protected_token(link)

    def test_private_files_and_consumer_isolation(self):
        config, values = fixture_config(), fixture_values()
        with tempfile.TemporaryDirectory() as directory:
            pilot.render_credentials(config, values, directory, bootstrap=True)
            root = Path(directory)
            self.assertEqual((root / "backend/POSTGRES_APP_PASSWORD").read_text(), values["db_app_password"])
            self.assertNotIn(values["db_admin_password"], (root / "flyway/flyway.conf").read_text())
            self.assertNotIn("db_app_password_fixture", (root / "flyway/flyway.conf").read_text())
            self.assertIn("starter_backup", (root / "backup/pgpass").read_text())
            for filename in root.rglob("*"):
                self.assertEqual(filename.stat().st_mode & 0o777, 0o700 if filename.is_dir() else 0o600)
        with tempfile.TemporaryDirectory() as directory:
            pilot.render_credentials(config, values, directory)
            self.assertFalse((Path(directory) / "bootstrap").exists())

    def test_properties_and_pgpass_escape_passwords(self):
        self.assertEqual(pilot.pgpass("app", "a:b\\c"), "db:5432:fullstack_starter_db:app:a\\:b\\\\c\n")
        self.assertEqual(pilot.properties({"password": "a:b\\c"}), "password=a:b\\c\n")
        for value in (" leading", "trailing ", "new\nline", "${EXPANSION}"):
            with self.assertRaises(pilot.PilotError):
                pilot.properties({"password": value})

    def test_compose_does_not_inherit_shell_or_environment_files(self):
        with patch.dict(os.environ, {"POSTGRES_PASSWORD": "must-not-propagate", "DOPPLER_TOKEN": "must-not-propagate"}):
            command, environment = pilot.compose_command(fixture_config(), "/run/fixture")
        self.assertEqual(command[2:4], ["--env-file", "/dev/null"])
        self.assertNotIn("POSTGRES_PASSWORD", environment)
        self.assertNotIn("DOPPLER_TOKEN", environment)
        self.assertEqual(command.count("-f"), 4)
        with self.assertRaises(pilot.PilotError):
            pilot.verify_model({"services": {"db": {"environment": {"POSTGRES_PASSWORD": "fixture"}}}})
        pilot.verify_model({"services": {"backup": {"environment": {"BACKUP_INCLUDE_ROLE_PASSWORDS": "false"}}}})

    def test_inventory_distinguishes_unknown_never_and_expiring_without_reading_tokens(self):
        config = fixture_config()
        config["credentials"]["db_app_password"].update(expires_at="unknown")
        config["identities"]["sandbox"].update(expires_at="2030-01-01T00:00:00Z")
        config["credentials"]["db_backup_password"].update(review_at="2030-02-01T00:00:00Z")
        with patch.object(pilot, "protected_token", side_effect=AssertionError("must not read tokens")):
            metrics = pilot.inventory_metrics(config, now=123)
        self.assertIn('credential="db_app_password"} 0', metrics)
        self.assertIn('credential="db_admin_password"} 1', metrics)
        self.assertIn('credential="doppler_sandbox"} 1893456000.0', metrics)
        self.assertIn("prodenv_credential_review_timestamp_seconds", metrics)
        self.assertNotIn("/etc/", metrics)
        for spec in config["credentials"].values():
            self.assertNotIn(spec["key"], metrics)

    def test_delivery_failure_preserves_last_success_and_atomic_public_metrics(self):
        config = fixture_config()
        with tempfile.TemporaryDirectory() as directory:
            config["metrics_directory"] = directory
            pilot.record_delivery(config, True, now=100)
            pilot.record_delivery(config, False, now=200)
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o644)
            text = files[0].read_text()
            self.assertIn('prodenv_secret_delivery_success{project="fullstack-starter-stage"} 0', text)
            self.assertIn('prodenv_secret_delivery_last_success_timestamp_seconds{project="fullstack-starter-stage"} 100.0', text)


AGE_EXECUTABLE = os.environ.get("AGE_EXECUTABLE") or shutil.which("age")
DOPPLER_EXECUTABLE = os.environ.get("DOPPLER_EXECUTABLE") or shutil.which("doppler")


def recovery_fixture(config, root):
    config["age_executable"] = AGE_EXECUTABLE
    config["recovery"]["directory"] = str(root / "exports")
    identity = root / "recovery.key"
    keygen = str(Path(AGE_EXECUTABLE).with_name("age-keygen"))
    subprocess.run([keygen, "--output", str(identity)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    recipient = subprocess.run([keygen, "-y", str(identity)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True).stdout.strip()
    config["recovery"]["recipients"] = [recipient]
    return identity


@unittest.skipUnless(DOPPLER_EXECUTABLE, "pinned Doppler CLI is required for the local API contract test")
class DopplerCliTest(unittest.TestCase):
    def test_actual_cli_downloads_without_fallback_and_checks_response_scope(self):
        config, values = fixture_config(), fixture_values()
        config["doppler_executable"] = DOPPLER_EXECUTABLE
        requests = []
        response = {"DOPPLER_PROJECT": "sandbox", "DOPPLER_CONFIG": "stg", "DOPPLER_ENVIRONMENT": "stg",
                    **{spec["key"]: values[alias] for alias, spec in config["credentials"].items()}}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *arguments):
                pass

            def do_GET(self):
                requests.append(self.path)
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def runner(arguments, environment):
                return pilot.quiet_run(arguments + ["--api-host", f"http://127.0.0.1:{server.server_port}"], environment)
            with tempfile.TemporaryDirectory() as directory:
                actual = pilot.fetch_credentials(config, directory, runner, lambda path: "dp.st.fixture-token")
                self.assertEqual(actual, values)
                self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(len(requests), 1)
            self.assertIn("/secrets/download", requests[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(AGE_EXECUTABLE, "age is required for encrypted recovery tests")
class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = fixture_config()
        self.identity = recovery_fixture(self.config, self.root)

    def test_real_encrypted_export_offline_restore_and_private_rendering(self):
        values = fixture_values()
        snapshot = pilot.export_recovery(self.config, values)
        encrypted = snapshot.read_bytes()
        for value in values.values():
            self.assertNotIn(value.encode(), encrypted)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        with patch.object(pilot, "fetch_credentials", side_effect=AssertionError("Doppler must not be contacted")):
            restored = pilot.recover_values(self.config, snapshot, self.identity)
            self.assertEqual(restored, values)
            pilot.render_credentials(self.config, restored, self.root / "restored")
        self.assertEqual((self.root / "restored/backend/POSTGRES_APP_PASSWORD").read_text(), values["db_app_password"])
        self.assertEqual(list(snapshot.parent.glob("*.json")), [])

    def test_wrong_key_tampering_wrong_deployment_and_stale_snapshot_fail(self):
        snapshot = pilot.export_recovery(self.config, fixture_values())
        other_key = self.root / "other.key"
        subprocess.run([str(Path(AGE_EXECUTABLE).with_name("age-keygen")), "-o", str(other_key)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        with self.assertRaises(pilot.PilotError):
            pilot.recover_values(self.config, snapshot, other_key)
        self.config["deployment"] = "fullstack-starter-prod"
        with self.assertRaises(pilot.PilotError):
            pilot.recover_values(self.config, snapshot, self.identity)
        self.config["deployment"] = "fullstack-starter-stage"
        with self.assertRaises(pilot.PilotError):
            pilot.recover_values(self.config, snapshot, self.identity, now=time.time() + 49 * 3600)
        corrupted = bytearray(snapshot.read_bytes())
        corrupted[-1] ^= 1
        snapshot.write_bytes(corrupted)
        with self.assertRaises(pilot.PilotError):
            pilot.recover_values(self.config, snapshot, self.identity)

    def test_failed_encryption_does_not_replace_previous_backup_and_retention_is_bounded(self):
        previous = pilot.export_recovery(self.config, fixture_values())
        with patch.object(pilot, "crypt", side_effect=pilot.PilotError("fixture encryption failure")):
            with self.assertRaises(pilot.PilotError):
                pilot.export_recovery(self.config, fixture_values())
        self.assertTrue(previous.exists())
        for index in range(5):
            pilot.export_recovery(self.config, fixture_values(), now=time.time() + index)
        self.assertEqual(len(list(previous.parent.glob("*.age"))), self.config["recovery"]["keep_exports"])


@unittest.skipUnless(os.environ.get("FULLSTACK_STARTER_DATABASE_TEST") == "1", "opt-in disposable Docker database test")
class DatabasePilotTest(unittest.TestCase):
    def test_bootstrap_migrate_authenticate_and_read_only_backup(self):
        checkout = Path(os.environ["FULLSTACK_STARTER_CHECKOUT"]) / "templates/docker"
        project = "doppler-pilot-" + uuid4().hex[:10]
        config, values = fixture_config(), fixture_values()

        def run(arguments):
            result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=240)
            if result.returncode:
                self.fail("Disposable fixture failed: " + result.stderr)
            return result.stdout

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(AGE_EXECUTABLE, "Database gate requires the real age recovery round trip")
            identity = recovery_fixture(config, root)
            snapshot = pilot.export_recovery(config, values)
            with patch.object(pilot, "fetch_credentials", side_effect=AssertionError("Offline recovery contacted Doppler")):
                values = pilot.recover_values(config, snapshot, identity)
            secret_root = root / "secrets"
            secret_root.mkdir(mode=0o700)
            pilot.render_credentials(config, values, secret_root, bootstrap=True)
            shutil.copytree(checkout / "flyway/sql", root / "sql")
            model = {"services": {
                "db": {"image": "postgres:16", "environment": {"POSTGRES_DB": "fullstack_starter_db", "POSTGRES_PASSWORD_FILE": "/run/secrets/password"},
                       "volumes": [str(secret_root / "db") + ":/run/secrets:ro"], "tmpfs": ["/var/lib/postgresql/data"],
                       "healthcheck": {"test": ["CMD", "pg_isready", "-U", "postgres", "-d", "fullstack_starter_db"], "interval": "1s", "timeout": "5s", "retries": 60}},
                "bootstrap": {"image": "flyway/flyway:10.18.0", "command": "migrate", "profiles": ["tools"],
                              "environment": {"FLYWAY_PLACEHOLDER_PREFIX": "{{ ", "FLYWAY_PLACEHOLDER_SUFFIX": " }}"},
                              "volumes": [str(secret_root / "bootstrap/flyway.conf") + ":/flyway/conf/flyway.conf:ro", str(root / "sql") + ":/flyway/sql:ro"]},
                "flyway": {"image": "flyway/flyway:10.18.0", "command": "migrate", "profiles": ["tools"],
                           "environment": {"FLYWAY_PLACEHOLDER_PREFIX": "{{ ", "FLYWAY_PLACEHOLDER_SUFFIX": " }}"},
                           "volumes": [str(secret_root / "flyway/flyway.conf") + ":/flyway/conf/flyway.conf:ro", str(root / "sql") + ":/flyway/sql:ro"]},
                "provision": {"image": "postgres:16", "profiles": ["tools"],
                              "environment": {"PGHOST": "db", "PGUSER": "postgres", "PGDATABASE": "fullstack_starter_db", "PGPASSFILE": "/run/secrets/pgpass"},
                              "volumes": [str(secret_root / "bootstrap") + ":/run/secrets:ro", str(checkout / "db/provision-secrets.sql") + ":/provision.sql:ro"],
                              "entrypoint": ["psql", "-X", "--no-password", "--set", "ON_ERROR_STOP=1", "--file", "/provision.sql"]},
            }}
            for role in ("app", "migration", "backup"):
                password_file = root / (role + ".pgpass")
                password_file.write_text(pilot.pgpass(config["roles"][role], values[f"db_{role}_password"]))
                password_file.chmod(0o600)
                model["services"]["check-" + role] = {
                    "image": "postgres:16", "profiles": ["tools"],
                    "environment": {"PGHOST": "db", "PGUSER": config["roles"][role], "PGDATABASE": "fullstack_starter_db", "PGPASSFILE": "/run/pgpass"},
                    "volumes": [str(password_file) + ":/run/pgpass:ro"],
                }
            backup = model["services"]["check-backup"]
            backup["user"] = f"{os.getuid()}:{os.getgid()}"
            backup["environment"].update(BACKUP_PROJECT=project, BACKUP_INCLUDE_ROLE_PASSWORDS="false", PGPORT="5432")
            backup["volumes"].extend([str(checkout / "backup/export-postgres.sh") + ":/export.sh:ro", str(root / "database-exports") + ":/backup-staging"])
            (root / "database-exports").mkdir()
            compose = root / "compose.json"
            compose.write_text(json.dumps(model))
            command = ["docker", "compose", "--env-file", "/dev/null", "--project-name", project, "-f", str(compose)]
            try:
                run(command + ["up", "-d", "--wait", "--wait-timeout", "90", "db"])
                run(command + ["run", "--rm", "bootstrap"])
                run(command + ["run", "--rm", "provision"])
                (root / "sql/tables/V100004__pilot_privileges.sql").write_text("CREATE TABLE pilot_probe (id integer PRIMARY KEY);\n")
                run(command + ["run", "--rm", "flyway"])
                for role in ("app", "migration", "backup"):
                    output = run(command + ["run", "--rm", "check-" + role, "psql", "-XAt", "--no-password", "-c", "SELECT current_user"])
                    self.assertEqual(output.strip(), config["roles"][role])
                output = run(command + ["run", "--rm", "check-app", "psql", "-XAt", "--no-password", "-c",
                                        "INSERT INTO pilot_probe VALUES (1); SELECT has_schema_privilege(current_user, 'public', 'CREATE'), has_table_privilege(current_user, 'flyway_schema_history', 'INSERT');"])
                self.assertIn("f|f", output)
                output = run(command + ["run", "--rm", "check-backup", "psql", "-XAt", "--no-password", "-c",
                                        "SELECT count(*) FROM pilot_probe; SELECT has_table_privilege(current_user, 'pilot_probe', 'INSERT');"])
                self.assertEqual(output.strip(), "1\nf")
                run(command + ["run", "--rm", "check-backup", "sh", "/export.sh"])
                exported = root / "database-exports" / project / "postgres"
                self.assertGreater((exported / "database.dump").stat().st_size, 0)
                self.assertNotIn("PASSWORD", (exported / "globals.sql").read_text())
                run(command + ["run", "--rm", "provision"])
                run(command + ["run", "--rm", "flyway"])
            finally:
                run(command + ["down", "--volumes", "--remove-orphans"])


if __name__ == "__main__":
    unittest.main()