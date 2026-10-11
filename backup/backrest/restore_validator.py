#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


TERMINAL_STATUSES = {
    "STATUS_SUCCESS",
    "STATUS_WARNING",
    "STATUS_ERROR",
    "STATUS_SYSTEM_CANCELLED",
    "STATUS_USER_CANCELLED",
}


class ValidationError(RuntimeError):
    pass


class BackrestClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.authorization = f"Basic {credentials}"

    def call(self, method, payload):
        request = urllib.request.Request(
            f"{self.base_url}/v1.Backrest/{method}",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": self.authorization,
                "Connect-Protocol-Version": "1",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            raise ValidationError(
                f"Backrest {method} failed with HTTP {error.code}: {body}"
            ) from error
        except urllib.error.URLError as error:
            raise ValidationError(f"Cannot reach Backrest: {error.reason}") from error
        return json.loads(body) if body else {}

    def latest_snapshot(self, repository, plan):
        response = self.call(
            "ListSnapshots", {"repoId": repository, "planId": plan}
        )
        snapshots = response.get("snapshots", [])
        if not snapshots:
            raise ValidationError(f"No snapshots found for plan {plan!r}")
        return max(snapshots, key=lambda item: int(item.get("unixTimeMs", 0)))

    def list_files(self, repository, snapshot, path):
        response = self.call(
            "ListSnapshotFiles",
            {"repoId": repository, "snapshotId": snapshot, "path": path},
        )
        return response.get("entries", [])

    def restore(self, repository, plan, snapshot, path, target):
        response = self.call(
            "Restore",
            {
                "repoId": repository,
                "planId": plan,
                "snapshotId": snapshot,
                "path": path,
                "target": target,
            },
        )
        operation_id = response.get("operationId")
        if operation_id in (None, "", 0, "0"):
            raise ValidationError("Backrest did not create a restore operation")
        return int(operation_id)

    def wait_for_operation(self, operation_id, timeout_seconds, poll_seconds=10):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self.call(
                "GetOperations", {"selector": {"ids": [str(operation_id)]}}
            )
            operations = response.get("operations", [])
            operation = next(
                (item for item in operations if int(item.get("id", 0)) == operation_id),
                None,
            )
            if operation and operation.get("status") in TERMINAL_STATUSES:
                if operation["status"] not in {"STATUS_SUCCESS", "STATUS_WARNING"}:
                    message = operation.get("displayMessage", "unknown error")
                    raise ValidationError(f"Backrest restore failed: {message}")
                return operation
            time.sleep(poll_seconds)
        self.call("Cancel", {"operationId": str(operation_id)})
        raise ValidationError(
            f"Backrest restore exceeded {timeout_seconds} seconds and was cancelled"
        )


def read_secret(path):
    try:
        value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
        raise ValidationError(f"Cannot read secret {path}: {error}") from error
    if not value:
        raise ValidationError(f"Secret {path} is empty")
    return value


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"Cannot load validator config {path}: {error}") from error
    entries = config.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("Restore validation whitelist must contain an entry")
    projects = set()
    for entry in entries:
        project = entry.get("project", "")
        path_value = entry.get("path", "")
        if (
            not project
            or project in projects
            or not path_value.startswith("/userdata/")
            or ".." in Path(path_value).parts
        ):
            raise ValidationError(f"Invalid whitelist entry: {entry!r}")
        projects.add(project)
    return config


def dump_size(entries, path):
    dumps = [entry for entry in entries if entry.get("name") == "database.dump"]
    checksums = [entry for entry in entries if entry.get("name") == "checksums.sha256"]
    if len(dumps) != 1 or len(checksums) != 1:
        raise ValidationError(
            f"{path} must contain one database.dump and one checksums.sha256"
        )
    return int(dumps[0].get("size", 0))


def check_guardrails(entry, size, restore_root):
    maximum = int(entry["maxDumpBytes"])
    if size <= 0 or size > maximum:
        raise ValidationError(
            f"Dump size {size} bytes is outside the allowed range 1..{maximum}"
        )
    multiplier = int(entry.get("freeSpaceMultiplier", 6))
    reserve = int(entry.get("minimumFreeBytes", 10 * 1024**3))
    required = max(reserve, size * multiplier)
    available = shutil.disk_usage(restore_root).free
    if available < required:
        raise ValidationError(
            f"Only {available} bytes are free; validation requires {required}"
        )


def find_restored_export(target):
    dumps = list(target.rglob("database.dump"))
    if len(dumps) != 1:
        raise ValidationError(
            f"Expected one restored database.dump in {target}, found {len(dumps)}"
        )
    export_directory = dumps[0].parent
    checksum_path = export_directory / "checksums.sha256"
    if not checksum_path.is_file():
        raise ValidationError("Restored export has no checksums.sha256")
    return export_directory


def verify_checksums(export_directory):
    for line in (export_directory / "checksums.sha256").read_text(
        encoding="utf-8"
    ).splitlines():
        expected, name = line.split(maxsplit=1)
        name = name.lstrip("* ")
        candidate = export_directory / name
        if candidate.parent != export_directory or not candidate.is_file():
            raise ValidationError(f"Invalid checksum entry: {name}")
        digest = hashlib.sha256()
        with candidate.open("rb") as restored_file:
            for chunk in iter(lambda: restored_file.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise ValidationError(f"Checksum failed for {name}")


def run_command(arguments, timeout_seconds):
    try:
        return subprocess.run(
            arguments,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"Command timed out: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise ValidationError(f"Command failed: {detail}") from error


def validate_database(dump_file, timeout_seconds):
    common = ["psql", "--host", os.environ["PGHOST"], "--username", "postgres"]
    run_command(
        common
        + [
            "--dbname",
            "postgres",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "DROP DATABASE IF EXISTS restore_validation WITH (FORCE);",
        ],
        60,
    )
    run_command(
        common
        + [
            "--dbname",
            "postgres",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "CREATE DATABASE restore_validation;",
        ],
        60,
    )
    try:
        run_command(
            [
                "pg_restore",
                "--host",
                os.environ["PGHOST"],
                "--username",
                "postgres",
                "--dbname",
                "restore_validation",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                str(dump_file),
            ],
            timeout_seconds,
        )
        result = run_command(
            common
            + [
                "--dbname",
                "restore_validation",
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');",
            ],
            60,
        )
        if int(result.stdout.strip()) <= 0:
            raise ValidationError("Restored database contains no application tables")
    finally:
        run_command(
            common
            + [
                "--dbname",
                "postgres",
                "--command",
                "DROP DATABASE IF EXISTS restore_validation WITH (FORCE);",
            ],
            60,
        )


def read_metric_state(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_metrics(metrics_directory, entries, state):
    metrics_directory.mkdir(parents=True, exist_ok=True)
    state_path = metrics_directory / "restore-validation-state.json"
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    lines = [
        "# HELP prodenv_backup_restore_validation_enabled Restore validation whitelist membership.",
        "# TYPE prodenv_backup_restore_validation_enabled gauge",
        "# HELP prodenv_backup_restore_validation_last_attempt_timestamp_seconds Last automated restore validation attempt.",
        "# TYPE prodenv_backup_restore_validation_last_attempt_timestamp_seconds gauge",
        "# HELP prodenv_backup_restore_validation_last_attempt_success Whether the last automated restore validation succeeded.",
        "# TYPE prodenv_backup_restore_validation_last_attempt_success gauge",
        "# HELP prodenv_backup_restore_validation_last_success_timestamp_seconds Last successful automated restore validation.",
        "# TYPE prodenv_backup_restore_validation_last_success_timestamp_seconds gauge",
    ]
    for entry in entries:
        project = entry["project"]
        labels = f'project="{project}"'
        lines.append(f"prodenv_backup_restore_validation_enabled{{{labels}}} 1")
        project_state = state.get(project, {})
        if "lastAttempt" in project_state:
            lines.append(
                f"prodenv_backup_restore_validation_last_attempt_timestamp_seconds{{{labels}}} {project_state['lastAttempt']}"
            )
            lines.append(
                f"prodenv_backup_restore_validation_last_attempt_success{{{labels}}} {project_state['lastAttemptSuccess']}"
            )
        if "lastSuccess" in project_state:
            lines.append(
                f"prodenv_backup_restore_validation_last_success_timestamp_seconds{{{labels}}} {project_state['lastSuccess']}"
            )
    temporary = metrics_directory / ".restore-validation.prom.tmp"
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(metrics_directory / "restore-validation.prom")


def validate_entry(client, config, entry, restore_root):
    snapshot = client.latest_snapshot(config["repository"], config["plan"])
    snapshot_id = snapshot["id"]
    entries = client.list_files(config["repository"], snapshot_id, entry["path"])
    check_guardrails(entry, dump_size(entries, entry["path"]), restore_root)
    target = restore_root / entry["project"]
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    try:
        operation_id = client.restore(
            config["repository"],
            config["plan"],
            snapshot_id,
            entry["path"],
            str(target),
        )
        client.wait_for_operation(operation_id, int(entry["restoreTimeoutSeconds"]))
        export_directory = find_restored_export(target)
        verify_checksums(export_directory)
        validate_database(
            export_directory / "database.dump", int(entry["importTimeoutSeconds"])
        )
    finally:
        shutil.rmtree(target, ignore_errors=True)


def run_once(client, config, restore_root, metrics_directory, now=None):
    timestamp = int(now if now is not None else time.time())
    state_path = metrics_directory / "restore-validation-state.json"
    state = read_metric_state(state_path)
    interval_seconds = int(config.get("intervalDays", 90)) * 86400
    failures = []
    write_metrics(metrics_directory, config["entries"], state)
    for entry in config["entries"]:
        project = entry["project"]
        last_success = int(state.get(project, {}).get("lastSuccess", 0))
        if last_success and timestamp - last_success < interval_seconds:
            continue
        try:
            validate_entry(client, config, entry, restore_root)
        except Exception as error:
            failures.append(f"{project}: {error}")
            state[project] = {
                **state.get(project, {}),
                "lastAttempt": timestamp,
                "lastAttemptSuccess": 0,
            }
        else:
            state[project] = {
                "lastAttempt": timestamp,
                "lastAttemptSuccess": 1,
                "lastSuccess": timestamp,
            }
        write_metrics(metrics_directory, config["entries"], state)
    if failures:
        raise ValidationError("; ".join(failures))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    try:
        config = load_config(
            os.environ.get("RESTORE_VALIDATOR_CONFIG", "/validator/config.json")
        )
        config["repository"] = os.environ.get(
            "BACKREST_REPOSITORY_ID", config.get("repository", "")
        )
        if not config["repository"]:
            raise ValidationError("BACKREST_REPOSITORY_ID is empty")
        restore_root = Path(os.environ.get("RESTORE_ROOT", "/restore/automated"))
        metrics_directory = Path(os.environ.get("METRICS_DIRECTORY", "/metrics"))
        restore_root.mkdir(parents=True, exist_ok=True)
        client = BackrestClient(
            os.environ.get("BACKREST_URL", "http://backrest:9898"),
            os.environ["BACKREST_ADMIN_USERNAME"],
            read_secret(
                os.environ.get(
                    "BACKREST_ADMIN_PASSWORD_FILE",
                    "/run/secrets/backrest_admin_password",
                )
            ),
        )
        while True:
            try:
                run_once(client, config, restore_root, metrics_directory)
            except ValidationError as error:
                print(f"Restore validation failed: {error}", file=sys.stderr, flush=True)
                if arguments.once:
                    return 1
            if arguments.once:
                return 0
            time.sleep(int(config.get("checkIntervalSeconds", 21600)))
    except (KeyError, ValidationError) as error:
        print(f"Restore validator configuration failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())