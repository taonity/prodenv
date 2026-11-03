#!/usr/bin/env python3

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


MANAGED_TAG = "managed-by:prodenv-retained-v1"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n")
    temporary.replace(path)


def durable_touch(path):
    with path.open("ab") as stream:
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def verify(directory):
    listed = set()
    for line in (directory / "checksums.sha256").read_text().splitlines():
        checksum, filename = line.split(maxsplit=1)
        if Path(filename).name != filename or filename in listed:
            raise ValueError("Invalid checksum filename")
        listed.add(filename)
        if digest(directory / filename) != checksum:
            raise ValueError(f"Checksum mismatch: {filename}")
    actual = {path.name for path in directory.iterdir() if path.is_file()} - {"checksums.sha256"}
    if actual != listed or "metadata.json" not in listed:
        raise ValueError("Incomplete checksum manifest")


def run(arguments, config, **kwargs):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=config["commandTimeoutSeconds"], **kwargs).stdout


def restic(arguments, config):
    env = {**os.environ, "RESTIC_REPOSITORY": os.environ["BACKREST_REPOSITORY_URI"].rstrip("/") + config["repositorySuffix"], "RESTIC_PASSWORD_FILE": os.environ.get("RESTIC_PASSWORD_FILE", "/run/secrets/backrest_repository_password"), "AWS_SHARED_CREDENTIALS_FILE": os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "/run/secrets/backrest_aws_credentials")}
    return run(["restic", "--retry-lock", "2m"] + arguments, config, env=env)


@contextmanager
def maintenance(config):
    directory = Path(config["stateDirectory"])
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        initialized = directory / "maintenance.initialized"
        pending = directory / "prune.pending"
        if not initialized.exists():
            if (directory / "expiry.json").exists():
                durable_touch(pending)
            initialized.touch()
        yield pending


def forget_snapshots(snapshot_ids, config):
    if snapshot_ids:
        with maintenance(config) as pending:
            durable_touch(pending)
            restic(["forget"] + snapshot_ids, config)


def prune_pending(config):
    with maintenance(config) as pending:
        if pending.exists():
            restic(["prune", "--max-unused", "0"], config)
            pending.unlink()


def tags(snapshot):
    return dict(tag.split(":", 1) for tag in snapshot.get("tags", []) if ":" in tag)


def expiry(snapshot, config, policies=None):
    values = tags(snapshot)
    stamp = int(values["expires-at"])
    if policies and (values["project"], values["dataset"]) in policies:
        start = int(datetime.fromisoformat(values["record-date"]).replace(tzinfo=timezone.utc).timestamp())
        stamp = min(stamp, start + policies[(values["project"], values["dataset"])])
    return stamp - config["expirySafetySeconds"]


def current_policies(config):
    result = {}
    for project in config["allowedProjects"]:
        path = Path(config["stagingRoot"]) / project / "policy.json"
        if not path.exists():
            path = Path(config["stagingRoot"]) / project / "current/metadata.json"
        if path.exists():
            data = json.loads(path.read_text())
            for dataset, seconds in data["retentionSeconds"].items():
                result[(project, dataset)] = int(seconds)
    return result


def expire(config):
    now = time.time()
    policies = current_policies(config)
    snapshots = json.loads(restic(["snapshots", "--json", "--tag", MANAGED_TAG], config)) or []
    expired = [snapshot["id"] for snapshot in snapshots if expiry(snapshot, config, policies) <= now]
    forget_snapshots(expired, config)
    prune_pending(config)
    root = Path(config["stagingRoot"])
    projects = set(config["allowedProjects"])
    if root.exists():
        projects.update(path.name for path in root.iterdir() if path.is_dir() and (path / ".backup.lock").is_file())
    for project in sorted(projects):
        project_root = root / project
        project_root.mkdir(parents=True, exist_ok=True)
        with (project_root / ".backup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for metadata in project_root.glob("current/*/*/metadata.json"):
                data = json.loads(metadata.read_text())
                deadline = int(data["expiresAt"])
                if (project, data["dataset"]) in policies:
                    start = int(datetime.fromisoformat(data["recordDate"]).replace(tzinfo=timezone.utc).timestamp())
                    deadline = min(deadline, start + policies[(project, data["dataset"])])
                if deadline - config["expirySafetySeconds"] <= now:
                    shutil.rmtree(metadata.parent)
            for previous in list(project_root.rglob("*.previous")) + list(project_root.glob(".export-*")) + list(project_root.glob(".alerts-*")):
                shutil.rmtree(previous)
    if "stateDirectory" in config:
        state = Path(config["stateDirectory"])
        state.mkdir(parents=True, exist_ok=True)
        with (state / "validation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return len(expired)
            scratch = state / "restore"
            pending = state / "validation.pending"
            if pending.exists() or (scratch.exists() and any(scratch.iterdir())):
                run(["psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c", "DROP DATABASE IF EXISTS retained_validation WITH (FORCE)"], config)
                if scratch.exists():
                    for path in scratch.iterdir():
                        shutil.rmtree(path)
                pending.unlink(missing_ok=True)
    return len(expired)


def restore_check(directory, metadata, config):
    verify(directory)
    if time.time() >= metadata["expiresAt"] - config["expirySafetySeconds"]:
        raise ValueError("Refusing to restore an expired cohort")
    if metadata["format"] != "postgres-csv-v1":
        raise ValueError("Unsupported retained export format")
    size = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
    if size > config["maxRestoreBytes"] or shutil.disk_usage(directory).free < size * 3:
        raise ValueError("Retained restore exceeds disk or size guardrails")
    common = ["psql", "-X", "-U", "postgres", "-v", "ON_ERROR_STOP=1"]
    run(common + ["-d", "postgres", "-c", "DROP DATABASE IF EXISTS retained_validation WITH (FORCE)"], config)
    run(common + ["-d", "postgres", "-c", "CREATE DATABASE retained_validation"], config)
    try:
        run(["pg_restore", "-U", "postgres", "--exit-on-error", "--no-owner", "--no-privileges", "-d", "retained_validation", str(directory / "database.dump")], config)
        run(common + ["-d", "retained_validation", "-f", "restore.sql"], config, cwd=directory)
    finally:
        run(common + ["-d", "postgres", "-c", "DROP DATABASE IF EXISTS retained_validation WITH (FORCE)"], config)


def backup_cohort(directory, metadata, snapshots, config):
    now = time.time()
    start = int(datetime.fromisoformat(metadata["recordDate"]).replace(tzinfo=timezone.utc).timestamp())
    if metadata["expiresAt"] != start + metadata["retentionSeconds"] or metadata["retentionSeconds"] <= 0:
        raise ValueError("Invalid record-age expiration metadata")
    if metadata["expiresAt"] - config["expirySafetySeconds"] <= now:
        return None
    if now - metadata["createdAt"] > config["maxExportAgeSeconds"] or metadata["createdAt"] > now + 60:
        raise ValueError("Retained export is stale or dated in the future")
    fingerprint = digest(directory / "checksums.sha256")
    matching = [snapshot for snapshot in snapshots if all(tags(snapshot).get(key) == str(metadata[field]) for key, field in (("project", "project"), ("dataset", "dataset"), ("record-date", "recordDate")))]
    identical = next((snapshot for snapshot in matching if tags(snapshot).get("checksum") == fingerprint), None)
    if identical:
        forget_snapshots([snapshot["id"] for snapshot in matching if snapshot["id"] != identical["id"]], config)
        return identical["id"]
    verify(directory)
    tag_values = [MANAGED_TAG, f"created-by:{os.environ['BACKREST_INSTANCE']}", f"plan:{metadata['project']}-{metadata['dataset']}", f"project:{metadata['project']}", f"dataset:{metadata['dataset']}", f"record-date:{metadata['recordDate']}", f"expires-at:{metadata['expiresAt']}", f"checksum:{fingerprint}"]
    arguments = ["backup", "--json", "--host", os.environ["BACKREST_INSTANCE"]]
    for tag in tag_values:
        arguments += ["--tag", tag]
    result = restic(arguments + [str(directory)], config)
    summary = next(json.loads(line) for line in result.splitlines() if json.loads(line).get("message_type") == "summary")
    snapshot_id = summary["snapshot_id"]
    forget_snapshots([snapshot["id"] for snapshot in matching], config)
    return snapshot_id


def write_metrics(config, state, expiry_only=False):
    directory = Path(config["metricsDirectory"])
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    if expiry_only:
        for key, name in (("lastAttempt", "last_attempt_timestamp_seconds"), ("lastSuccess", "last_success_timestamp_seconds"), ("success", "success")):
            lines.append(f"prodenv_backup_retention_{name} {state.get(key, 0)}")
        for project in config.get("expectedProjects", []):
            path = Path(config["stagingRoot"]) / project / "policy.json"
            stamp = json.loads(path.read_text())["updatedAt"] if path.exists() else 0
            lines.append(f'prodenv_backup_policy_timestamp_seconds{{project="{project}"}} {stamp}')
    else:
        lines.append(f"prodenv_backup_retained_worker_success {state.get('workerSuccess', 0)}")
        lines.append(f"prodenv_backup_retained_worker_last_run_timestamp_seconds {state.get('workerTimestamp', 0)}")
        expected = {(project, dataset) for project in config["expectedProjects"] for dataset in ("core", "chat", "audit")}
        for key in state:
            if "/" in key:
                project, dataset = key.split("/", 1)
                if project in config["allowedProjects"] and dataset in ("core", "chat", "audit"):
                    expected.add((project, dataset))
        for project, dataset in sorted(expected):
            labels = f'project="{project}",dataset="{dataset}"'
            values = state.get(f"{project}/{dataset}", {})
            lines.append(f"prodenv_backup_dataset_enabled{{{labels}}} 1")
            for key, name in (("exportTimestamp", "export_timestamp_seconds"), ("snapshotTimestamp", "snapshot_timestamp_seconds"), ("validationTimestamp", "validation_timestamp_seconds"), ("success", "success")):
                lines.append(f"prodenv_backup_dataset_{name}{{{labels}}} {values.get(key, 0)}")
    path = directory / ("retained-expiry.prom" if expiry_only else "retained-backup.prom")
    temporary = path.with_suffix(".tmp")
    families = sorted({line.split("{", 1)[0].split()[0] for line in lines})
    headers = [f"# HELP {name} {name.replace('_', ' ')}.\n# TYPE {name} gauge" for name in families]
    temporary.write_text("\n".join(headers + lines) + "\n")
    temporary.chmod(0o644)
    temporary.replace(path)


def validation_due(config, values, now):
    interval = config["validationIntervalSeconds"]
    return int(now) // interval > values.get("validationTimestamp", 0) // interval


def cycle(config, state):
    failures = []
    for key in list(state):
        if "/" in key:
            project, dataset = key.split("/", 1)
            if project not in config["allowedProjects"] or dataset not in ("core", "chat", "audit"):
                del state[key]
    snapshots = json.loads(restic(["snapshots", "--json", "--tag", MANAGED_TAG], config)) or []
    for project in config["allowedProjects"]:
        path = Path(config["coreRoot"]) / project / ".last-success"
        if path.exists() or project in config["expectedProjects"]:
            state[f"{project}/core"] = {"exportTimestamp": int(path.read_text()) if path.exists() else 0, "success": int(path.exists())}
    for project in config["allowedProjects"]:
        root = Path(config["stagingRoot"]) / project
        if not root.exists():
            continue
        with (root / ".backup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            for dataset in ("chat", "audit"):
                directories = sorted((root / "current" / dataset).glob("*/metadata.json"))
                if not directories:
                    continue
                key = f"{project}/{dataset}"
                values = state.setdefault(key, {})
                try:
                    feed = root / "policy.json"
                    if not feed.exists() or time.time() - json.loads(feed.read_text())["updatedAt"] > config["policyMaxAgeSeconds"]:
                        raise ValueError("Retention policy feed is stale; upload refused")
                    newest = None
                    for path in directories:
                        metadata = json.loads(path.read_text())
                        if metadata["project"] != project or metadata["dataset"] != dataset:
                            raise ValueError("Cohort identity does not match its staging path")
                        effective = current_policies(config).get((project, dataset))
                        if effective is not None and effective != metadata["retentionSeconds"]:
                            raise ValueError("Awaiting exports for the updated retention policy")
                        snapshot_id = backup_cohort(path.parent, metadata, snapshots, config)
                        if snapshot_id:
                            newest = (metadata, snapshot_id, path.parent)
                    if not newest:
                        raise ValueError("No unexpired cohort is available")
                    metadata, snapshot_id, source = newest
                    values.update(exportTimestamp=metadata["createdAt"], snapshotTimestamp=int(time.time()), success=1)
                    if validation_due(config, values, time.time()):
                        state_root = Path(config["stateDirectory"])
                        scratch = state_root / "restore"
                        scratch.mkdir(parents=True, exist_ok=True)
                        with (state_root / "validation.lock").open("a") as validation_lock:
                            fcntl.flock(validation_lock, fcntl.LOCK_EX)
                            pending = state_root / "validation.pending"
                            durable_touch(pending)
                            with tempfile.TemporaryDirectory(prefix="cohort-", dir=scratch) as temporary:
                                restic(["restore", snapshot_id, "--target", temporary], config)
                                restored = Path(temporary) / str(source).lstrip("/")
                                restored_metadata = json.loads((restored / "metadata.json").read_text())
                                restore_check(restored, restored_metadata, config)
                            pending.unlink()
                        values["validationTimestamp"] = int(time.time())
                except Exception as error:
                    values["success"] = 0
                    failures.append(f"{key}: {error}")
    if failures:
        raise RuntimeError("; ".join(failures))


def backup_delay(config, state, now):
    value = config["backupScheduleTimeUtc"]
    scheduled = datetime.strptime(value, "%H:%M")
    if scheduled.strftime("%H:%M") != value:
        raise ValueError("backupScheduleTimeUtc must use 24-hour HH:MM format")
    today = now.date().isoformat()
    due = now.replace(hour=scheduled.hour, minute=scheduled.minute, second=0, microsecond=0)
    if state.get("lastScheduledDate") == today:
        due += timedelta(days=1)
    return max(0, (due - now).total_seconds())


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/retained-worker/config.json")
    parser.add_argument("--expiry-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    config = json.loads(Path(options.config).read_text())
    if config["commandTimeoutSeconds"] + config["expiryIntervalSeconds"] >= config["expirySafetySeconds"]:
        raise ValueError("Expiry safety must exceed command timeout plus sweep interval")
    state_file = Path(config["stateDirectory"]) / ("expiry.json" if options.expiry_only else "backup.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    while True:
        if not options.expiry_only and not options.once:
            delay = backup_delay(config, state, datetime.now(timezone.utc))
            if delay:
                time.sleep(delay)
                continue
        success = True
        try:
            if options.expiry_only:
                state.update(lastAttempt=int(time.time()))
                write_metrics(config, state, True)
                expire(config)
                state.update(lastSuccess=int(time.time()), success=1)
            else:
                state.update(workerTimestamp=int(time.time()), lastScheduledDate=datetime.now(timezone.utc).date().isoformat())
                atomic_json(state_file, state)
                write_metrics(config, state)
                cycle(config, state)
                state["workerSuccess"] = 1
        except Exception as error:
            success = False
            state["success" if options.expiry_only else "workerSuccess"] = 0
            print(f"Retained backup operation failed: {error}", flush=True)
        atomic_json(state_file, state)
        write_metrics(config, state, options.expiry_only)
        if options.once:
            return 0 if success else 1
        if options.expiry_only:
            time.sleep(config["expiryIntervalSeconds"])


if __name__ == "__main__":
    raise SystemExit(main())
