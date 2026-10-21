import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


CREDENTIAL_IDENTITIES = {
    "db_admin_password": "db",
    "db_app_password": "backend",
    "google_client_id": "backend",
    "google_client_secret": "backend",
    "db_migration_password": "flyway",
    "db_backup_password": "backup",
}
SANDBOX_PREFIXES = (
    "FULLSTACK_STARTER_STAGE_", "FULLSTACK_STARTER_PROD_", "SINAIR_LLM_BOT_STAGE_",
    "ARTIST_INSIGHT_SERVICE_STAGE_", "GENTOOL_DATA_VIEWER_STAGE_",
)
DOPPLER_VERSION = "3.77.0"
AGE_VERSION = "1.3.2"


class PilotError(Exception):
    pass


def timestamp(value):
    if value in ("unknown", "never"):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PilotError("Expiry timestamps must include a timezone")
    return parsed.timestamp()


def validate_config(config):
    if config["deployment"] not in ("fullstack-starter-stage", "fullstack-starter-prod"):
        raise PilotError("This pilot supports only Fullstack Starter stage and prod")
    roles = config["roles"]
    if set(roles) != {"admin", "app", "migration", "backup"}:
        raise PilotError("All four database roles must be configured")
    if len(set(roles.values())) != 4 or any(not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name) for name in roles.values()):
        raise PilotError("Database roles must be distinct lower-case SQL identifiers")
    if set(config["credentials"]) != set(CREDENTIAL_IDENTITIES):
        raise PilotError("Configure exactly the six pilot credentials")
    if set(config["identities"]) != {"sandbox"}:
        raise PilotError("Fullstack Starter stage and demo must use the shared sandbox config")
    identifiers = []
    prefix = config["deployment"].replace("-", "_").upper() + "_"
    for credential in config["credentials"].values():
        if credential["source"] != "sandbox" or not re.fullmatch(prefix + r"[A-Z][A-Z0-9_]*", credential["key"]):
            raise PilotError("Pilot secret names must be namespaced to the selected deployment")
        identifiers.append((credential["source"], credential["key"]))
        timestamp(credential["expires_at"])
        timestamp(credential["review_at"])
    if len(set(identifiers)) != len(identifiers):
        raise PilotError("Each credential must use a distinct Doppler key")
    for identity in config["identities"].values():
        token = Path(identity["token_file"])
        if token != Path("/etc/prodenv-doppler/tokens/sandbox"):
            raise PilotError("Store the sandbox service token under /etc/prodenv-doppler/tokens/sandbox")
        if identity["project"] != "sandbox" or identity["config"] != "stg":
            raise PilotError("The shared sandbox must use the sandbox project and stg config")
        timestamp(identity["expires_at"])
    for key in ("checkout", "host_override", "metrics_directory", "doppler_executable", "age_executable", "state_directory"):
        if not Path(config[key]).is_absolute():
            raise PilotError("Deployment paths must be absolute")
    if "template_directory" in config and not Path(config["template_directory"]).is_absolute():
        raise PilotError("The deployment template directory must be absolute")
    recovery = config["recovery"]
    if not Path(recovery["directory"]).is_absolute():
        raise PilotError("Recovery export directory must be absolute")
    if not recovery["recipients"] or any(not re.fullmatch(r"age1[0-9a-z]+", recipient) for recipient in recovery["recipients"]):
        raise PilotError("Configure age public recovery recipients, never private keys")
    if not 1 <= recovery["max_age_hours"] <= 720 or not 2 <= recovery["keep_exports"] <= 100:
        raise PilotError("Configure bounded recovery age and export retention")
    return config


def quiet_run(arguments, environment=None, timeout=120):
    try:
        result = subprocess.run(arguments, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PilotError("Subprocess unavailable or timed out; output suppressed") from None
    if result.returncode:
        raise PilotError("Subprocess failed; secret-bearing output suppressed")
    return result.stdout


def protected_token(filename):
    path = Path(filename)
    for parent in (path.parent, *path.parents):
        if parent.is_symlink() or parent.stat().st_mode & 0o022:
            raise PilotError("Token ancestors must not be symlinks or writable by other users")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        information = os.fstat(handle.fileno())
        if not stat.S_ISREG(information.st_mode) or information.st_uid != os.geteuid() or information.st_mode & 0o077:
            raise PilotError("Token files must be owner-only regular files owned by the delivery user")
        token = handle.read().strip()
    if not token or "\n" in token or "\r" in token:
        raise PilotError("Machine token file is empty or malformed")
    return token


def fetch_credentials(config, directory, runner=quiet_run, token_reader=protected_token):
    values = {}
    for identity_name, identity in config["identities"].items():
        expiry = timestamp(identity["expires_at"])
        if expiry is not None and expiry <= time.time():
            raise PilotError("A configured machine token has expired")
        token = token_reader(identity["token_file"])
        if not token.startswith("dp.st."):
            raise PilotError("Use a config-scoped Doppler service token, provisioned with read-only access")
        with tempfile.TemporaryDirectory(prefix="doppler-", dir=directory) as home:
            environment = {"PATH": os.defpath, "HOME": home, "XDG_CONFIG_HOME": home,
                           "DOPPLER_TOKEN": token}
            output = runner([config["doppler_executable"], "secrets", "download", "--no-file",
                             "--format", "json", "--no-fallback", "--no-check-version", "--config-dir", home,
                             "--project", identity["project"], "--config", identity["config"]], environment)
            try:
                if len(output.encode("utf-8")) > 500 * 1024:
                    raise PilotError("Doppler config exceeds the verified payload limit")
                secrets = json.loads(output)
                if secrets.get("DOPPLER_PROJECT") != identity["project"] or secrets.get("DOPPLER_CONFIG") != identity["config"] or secrets.get("DOPPLER_ENVIRONMENT") != identity["config"].split("_")[0]:
                    raise PilotError("Doppler returned an unexpected project or config")
                for key, value in secrets.items():
                    if key in ("DOPPLER_PROJECT", "DOPPLER_CONFIG", "DOPPLER_ENVIRONMENT"):
                        continue
                    if not key.startswith(SANDBOX_PREFIXES):
                        raise PilotError("Sandbox contains an unapproved credential namespace")
                    if not isinstance(value, str) or len(value.encode("utf-8")) > 50 * 1024:
                        raise PilotError("Doppler returned an invalid or oversized secret")
                if len(secrets) - 3 > 1200:
                    raise PilotError("Doppler config exceeds the verified secret count limit")
            except (AttributeError, TypeError, ValueError):
                raise PilotError("Invalid Doppler response; contents suppressed") from None
            for alias, spec in config["credentials"].items():
                if spec["source"] != identity_name:
                    continue
                expiry = timestamp(spec["expires_at"])
                if expiry is not None and expiry <= time.time():
                    raise PilotError("A configured provider credential has expired")
                try:
                    value = secrets[spec["key"]]
                    if not isinstance(value, str) or not value or any(character in value for character in "\x00\r\n"):
                        raise PilotError("Pilot credentials must be nonempty single-line values")
                    values[alias] = value
                except (KeyError, TypeError, ValueError):
                    raise PilotError("Missing or invalid Doppler secret; contents suppressed") from None
    passwords = [value for alias, value in values.items() if alias.startswith("db_")]
    if len(set(passwords)) != 4:
        raise PilotError("Database roles must not share passwords")
    return values


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    information = path.lstat()
    if not stat.S_ISDIR(information.st_mode) or information.st_uid != os.geteuid() or information.st_mode & 0o077:
        raise PilotError("Credential directories must be private real directories owned by the delivery user")
    for parent in path.parents:
        if parent.is_symlink() or (parent.stat().st_mode & 0o022 and not parent.stat().st_mode & stat.S_ISVTX):
            raise PilotError("Credential directory ancestors must not be writable by other users")
    return path


def crypt(config, arguments, payload):
    try:
        result = subprocess.run([config["age_executable"], *arguments], input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                                env={"PATH": os.defpath}, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PilotError("Recovery encryption tool unavailable or timed out") from None
    if result.returncode:
        raise PilotError("Recovery encryption or decryption failed; details suppressed")
    return result.stdout


def export_recovery(config, values, now=None):
    now = time.time() if now is None else now
    directory = private_directory(config["recovery"]["directory"]) / config["deployment"]
    directory = private_directory(directory)
    snapshot = {"schema": 1, "deployment": config["deployment"], "created_at": now,
                "roles": config["roles"], "credentials": config["credentials"], "values": values}
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    arguments = ["--encrypt"]
    for recipient in config["recovery"]["recipients"]:
        arguments.extend(["--recipient", recipient])
    encrypted = crypt(config, arguments, payload)
    if not encrypted.startswith(b"age-encryption.org/v1\n"):
        raise PilotError("Recovery encryption did not produce an age file")
    filename = directory / (str(int(now)) + "-" + uuid4().hex + ".age")
    descriptor, temporary = tempfile.mkstemp(prefix=".export-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    exports = sorted(directory.glob("*.age"), key=lambda path: path.stat().st_mtime, reverse=True)
    for expired in exports[config["recovery"]["keep_exports"]:]:
        expired.unlink()
    return filename


def recover_values(config, snapshot_file, identity_file, now=None):
    now = time.time() if now is None else now
    identity = Path(identity_file)
    information = identity.lstat()
    if not stat.S_ISREG(information.st_mode) or information.st_uid != os.geteuid() or information.st_mode & 0o077:
        raise PilotError("Recovery identity must be an owner-only regular file")
    snapshot_file = Path(snapshot_file)
    if snapshot_file.stat().st_size > 1024 * 1024:
        raise PilotError("Recovery snapshot is unexpectedly large")
    try:
        snapshot = json.loads(crypt(config, ["--decrypt", "--identity", str(identity)], snapshot_file.read_bytes()))
        if snapshot["schema"] != 1 or snapshot["deployment"] != config["deployment"] or snapshot["roles"] != config["roles"]:
            raise PilotError("Recovery snapshot does not match the selected deployment and roles")
        if type(snapshot["created_at"]) not in (int, float) or not math.isfinite(snapshot["created_at"]):
            raise PilotError("Recovery snapshot has an invalid timestamp")
        age = now - snapshot["created_at"]
        if age < -300 or age > config["recovery"]["max_age_hours"] * 3600:
            raise PilotError("Recovery snapshot is stale or dated in the future")
        values = snapshot["values"]
        if set(values) != set(config["credentials"]) or set(snapshot["credentials"]) != set(values):
            raise PilotError("Recovery snapshot credential set is incomplete")
        for alias, value in values.items():
            recorded = snapshot["credentials"][alias]
            if any(recorded[field] != config["credentials"][alias][field] for field in ("source", "key")):
                raise PilotError("Recovery snapshot has a different credential mapping")
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 50 * 1024 or any(character in value for character in "\x00\r\n"):
                raise PilotError("Recovery snapshot contains an invalid credential")
            for metadata in (recorded, config["credentials"][alias]):
                expiry = timestamp(metadata["expires_at"])
                if expiry is not None and expiry <= now:
                    raise PilotError("Recovery snapshot contains an expired provider credential")
        if len({value for alias, value in values.items() if alias.startswith("db_")}) != 4:
            raise PilotError("Recovered database roles must not share passwords")
        return values
    except (KeyError, TypeError, ValueError):
        raise PilotError("Invalid recovery snapshot; contents suppressed") from None


def record_export(config, success, now=None):
    now = time.time() if now is None else now
    labels = f'project="{config["deployment"]}"'
    metric = f'prodenv_secret_export_timestamp_seconds{{{labels}}}'
    previous = Path(config["metrics_directory"]) / f'doppler-{config["deployment"]}-export.prom'
    last_success = 0
    if previous.exists():
        for line in previous.read_text().splitlines():
            if line.startswith(metric + " "):
                last_success = float(line.split()[-1])
    text = ('# HELP prodenv_secret_export_timestamp_seconds Last successful encrypted recovery export.\n'
            '# TYPE prodenv_secret_export_timestamp_seconds gauge\n'
            '# HELP prodenv_secret_export_success Whether the latest recovery export attempt succeeded.\n'
            '# TYPE prodenv_secret_export_success gauge\n'
            f'{metric} {now if success else last_success}\n'
            f'prodenv_secret_export_success{{{labels}}} {int(success)}\n')
    write_metrics(config, "export", text)


def deployment_state(config, values):
    def fingerprint(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {
        "fingerprint": fingerprint({"roles": config["roles"], "values": values}),
        "database_fingerprint": fingerprint({"roles": config["roles"], "values": {name: value for name, value in values.items() if name.startswith("db_")}}),
    }


def check_update(previous, current, bootstrap=False, offline=False):
    if previous and previous.get("database_fingerprint") != current["database_fingerprint"] and not bootstrap and not offline:
        raise PilotError("Database credentials changed; confirmed maintenance bootstrap is required before replacing containers")
    return not previous or previous.get("fingerprint") != current["fingerprint"]


def save_state(filename, state):
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=filename.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle)
        os.replace(temporary, filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def properties(values):
    for value in values.values():
        if value != value.strip() or "${" in value or any(character in value for character in "\x00\r\n"):
            raise PilotError("Flyway credentials cannot contain surrounding whitespace, newlines or environment references")
    return "".join(f"{key}={value}\n" for key, value in values.items())


def pgpass(username, password):
    def escape(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")
    return f"db:5432:fullstack_starter_db:{escape(username)}:{escape(password)}\n"


def render_credentials(config, values, directory, owners=None, bootstrap=False):
    owners = owners or {}
    roles = config["roles"]
    flyway = {
        "flyway.url": "jdbc:postgresql://db:5432/fullstack_starter_db",
        "flyway.schemas": "public",
        "flyway.user": roles["migration"],
        "flyway.password": values["db_migration_password"],
        "flyway.placeholders.app_user": roles["app"],
        "flyway.placeholders.app_password": "unused-after-bootstrap",
        "flyway.placeholders.db_name": "fullstack_starter_db",
        "flyway.locations": "filesystem:/flyway/sql/init,filesystem:/flyway/sql/tables",
    }
    files = {
        "db/password": values["db_admin_password"],
        "backend/POSTGRES_APP_USER": roles["app"],
        "backend/POSTGRES_APP_PASSWORD": values["db_app_password"],
        "backend/GOOGLE_CLIENT_ID": values["google_client_id"],
        "backend/GOOGLE_CLIENT_SECRET": values["google_client_secret"],
        "flyway/flyway.conf": properties(flyway),
        "backup/pgpass": pgpass(roles["backup"], values["db_backup_password"]),
    }
    if bootstrap:
        flyway.update({"flyway.user": roles["admin"], "flyway.password": values["db_admin_password"],
                       "flyway.initSql": "SET log_statement = 'none'; SET log_min_error_statement = 'panic'; SET log_min_duration_statement = -1; SET log_min_duration_sample = -1",
                       "flyway.placeholders.app_password": values["db_app_password"].replace("'", "''")})
        files["bootstrap/flyway.conf"] = properties(flyway)
        files["bootstrap/pgpass"] = pgpass(roles["admin"], values["db_admin_password"])
        for role in ("app", "migration", "backup"):
            files[f"bootstrap/{role}_user"] = roles[role]
            files[f"bootstrap/{role}_password"] = values[f"db_{role}_password"]
    for relative, value in files.items():
        destination = Path(directory) / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="") as handle:
            handle.write(value)
        destination.chmod(0o600)
        owner = owners.get(relative, owners.get(relative.split("/")[0], os.geteuid()))
        os.chown(destination, owner, -1)
    for consumer in {relative.split("/")[0] for relative in files}:
        os.chown(Path(directory) / consumer, owners.get(consumer, os.geteuid()), -1)


def compose_command(config, directory):
    template = Path(config.get("template_directory", str(Path(config["checkout"]) / "templates/docker")))
    files = [template / "docker-compose.yml", Path(config["host_override"]),
             template / "docker-compose.prodenv.yml", template / "docker-compose.secrets.yml"]
    arguments = ["docker", "compose", "--env-file", "/dev/null", "--project-name", config["deployment"],
                 "--project-directory", str(template)]
    for filename in files:
        arguments.extend(["-f", str(filename)])
    profile = "prod" if config["deployment"].endswith("-prod") else "stage"
    environment = {"PATH": os.defpath, "HOME": "/root", "COMPOSE_CREDENTIALS_FILE": "credentials-files.yml",
                   "COMPOSE_PROJECT_NAME": config["deployment"], "SECRET_DIRECTORY": str(directory),
                   "SPRING_PROFILES_ACTIVE": profile, "FRONTEND_PROFILE": profile,
                   "POSTGRES_USER": config["roles"]["admin"], "POSTGRES_BACKUP_USER": config["roles"]["backup"]}
    return arguments, environment


def verify_model(model):
    for service in model["services"].values():
        if service.get("env_file"):
            raise PilotError("An environment file would be injected into a pilot container")
        for key in service.get("environment", {}):
            if re.search(r"PASSWORD|SECRET|TOKEN|CREDENTIAL", key) and not key.endswith("_FILE") and key != "BACKUP_INCLUDE_ROLE_PASSWORDS":
                raise PilotError("A credential environment variable remains in the Compose model")


def image_owner(service):
    user = service.get("user")
    if user is None:
        user = quiet_run(["docker", "image", "inspect", "--format", "{{.Config.User}}", service["image"]]).strip()
    if not user or user == "root":
        return 0
    if user.split(":")[0].isdigit():
        return int(user.split(":")[0])
    return int(quiet_run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "id", service["image"], "-u"]).strip())


def write_metrics(config, suffix, text):
    directory = Path(config["metrics_directory"])
    if not directory.is_dir():
        raise PilotError("The monitoring textfile directory must already exist")
    descriptor, temporary = tempfile.mkstemp(prefix=".doppler-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, directory / f"doppler-{config['deployment']}-{suffix}.prom")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inventory_metrics(config, now=None):
    now = time.time() if now is None else now
    project = config["deployment"]
    lines = [
        "# HELP prodenv_secret_inventory_timestamp_seconds Last successful metadata refresh in Unix seconds.",
        "# TYPE prodenv_secret_inventory_timestamp_seconds gauge",
        "# HELP prodenv_credential_expiry_known Whether a credential expiry policy is explicitly recorded.",
        "# TYPE prodenv_credential_expiry_known gauge",
        "# HELP prodenv_credential_expiry_timestamp_seconds Recorded credential expiry in Unix seconds.",
        "# TYPE prodenv_credential_expiry_timestamp_seconds gauge",
        "# HELP prodenv_credential_review_timestamp_seconds Recorded credential review deadline in Unix seconds.",
        "# TYPE prodenv_credential_review_timestamp_seconds gauge",
        f'prodenv_secret_inventory_timestamp_seconds{{project="{project}"}} {now}',
    ]
    entries = dict(config["credentials"])
    entries.update({"doppler_" + name: identity for name, identity in config["identities"].items()})
    for alias, entry in entries.items():
        labels = f'project="{project}",credential="{alias}"'
        lines.append(f'prodenv_credential_expiry_known{{{labels}}} {int(entry["expires_at"] != "unknown")}')
        expiry = timestamp(entry["expires_at"])
        if expiry is not None:
            lines.append(f'prodenv_credential_expiry_timestamp_seconds{{{labels}}} {expiry}')
        review = timestamp(entry.get("review_at", "unknown"))
        if review is not None:
            lines.append(f'prodenv_credential_review_timestamp_seconds{{{labels}}} {review}')
    return "\n".join(lines) + "\n"


def record_delivery(config, success, now=None):
    now = time.time() if now is None else now
    project = config["deployment"]
    labels = f'project="{project}"'
    last_success = f'prodenv_secret_delivery_last_success_timestamp_seconds{{{labels}}}'
    previous = Path(config["metrics_directory"]) / f"doppler-{project}-delivery.prom"
    successful_at = 0
    if previous.exists():
        for line in previous.read_text().splitlines():
            if line.startswith(last_success + " "):
                successful_at = float(line.split()[-1])
    text = ('# HELP prodenv_secret_delivery_expected Whether credential delivery has been activated for a project.\n'
            '# TYPE prodenv_secret_delivery_expected gauge\n'
            '# HELP prodenv_secret_delivery_success Whether the last credential deployment succeeded.\n'
            '# TYPE prodenv_secret_delivery_success gauge\n'
            '# HELP prodenv_secret_delivery_attempt_timestamp_seconds Last credential deployment attempt in Unix seconds.\n'
            '# TYPE prodenv_secret_delivery_attempt_timestamp_seconds gauge\n'
            '# HELP prodenv_secret_delivery_last_success_timestamp_seconds Last successful credential deployment in Unix seconds.\n'
            '# TYPE prodenv_secret_delivery_last_success_timestamp_seconds gauge\n'
            f'prodenv_secret_delivery_expected{{{labels}}} 1\n'
            f'prodenv_secret_delivery_success{{{labels}}} {int(success)}\n'
            f'prodenv_secret_delivery_attempt_timestamp_seconds{{{labels}}} {now}\n'
            f'{last_success} {now if success else successful_at}\n')
    write_metrics(config, "delivery", text)


def deploy(config, bootstrap=False, recovered=None, only_if_changed=False):
    if os.geteuid() != 0:
        raise PilotError("Run deployment as root on the Linux host")
    if recovered is None:
        version = quiet_run([config["doppler_executable"], "--version"]).strip().lstrip("v")
        if version != DOPPLER_VERSION:
            raise PilotError("Install the pinned Doppler CLI before deployment")
        if quiet_run([config["age_executable"], "--version"]).strip().lstrip("v") != AGE_VERSION:
            raise PilotError("Install the pinned age tool before deployment")
    root = Path("/run/prodenv-secrets")
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o077:
        raise PilotError("Runtime secret root must be a private, real directory")
    if quiet_run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(root)]).strip() != "tmpfs":
        raise PilotError("Runtime credentials require a tmpfs-backed /run directory")
    state_directory = private_directory(config["state_directory"])
    state_file = state_directory / (config["deployment"] + ".json")
    with (root / (config["deployment"] + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = Path(tempfile.mkdtemp(prefix=config["deployment"] + "-", dir=root))
        started = False
        try:
            values = recovered if recovered is not None else fetch_credentials(config, directory)
            if recovered is None:
                try:
                    export_recovery(config, values)
                    record_export(config, True)
                except (PilotError, OSError):
                    record_export(config, False)
                    raise
            previous = json.loads(state_file.read_text()) if state_file.exists() else {}
            current = deployment_state(config, values)
            changed = check_update(previous, current, bootstrap, recovered is not None)
            if only_if_changed and not changed and Path(previous.get("generation", "/nonexistent")).is_dir():
                print("Secrets unchanged; recovery export refreshed without restarting containers")
                return False
            command, environment = compose_command(config, directory)
            model = json.loads(quiet_run(command + ["--profile", "secrets-bootstrap", "config", "--format", "json"], environment))
            verify_model(model)
            if recovered is None:
                quiet_run(command + ["--profile", "secrets-bootstrap", "pull"], environment, timeout=600)
            mapping = {"db": "db", "backend": "backend", "flyway": "flyway", "backup": "postgres-export", "bootstrap": "secrets-provision"}
            owners = {consumer: image_owner(model["services"][service]) for consumer, service in mapping.items()}
            owners["bootstrap/flyway.conf"] = image_owner(model["services"]["secrets-migrate"])
            render_credentials(config, values, directory, owners, bootstrap)
            offline_flags = ["--pull", "never"] if recovered is not None else []
            started = True
            if bootstrap:
                quiet_run(command + ["stop", "frontend", "backend", "postgres-export"], environment)
            quiet_run(command + ["up", "-d", "--wait", "--wait-timeout", "180", *offline_flags, "db"], environment, timeout=240)
            if bootstrap:
                for service in ("secrets-migrate", "secrets-provision"):
                    quiet_run(command + ["--profile", "secrets-bootstrap", "run", "--rm", "--no-deps", *offline_flags, service], environment, timeout=600)
                shutil.rmtree(directory / "bootstrap")
            quiet_run(command + ["run", "--rm", "--no-deps", *offline_flags, "flyway"], environment, timeout=600)
            quiet_run(command + ["up", "-d", "--no-deps", "--force-recreate", *offline_flags, "backend", "frontend", "postgres-export"], environment, timeout=600)
            quiet_run(command + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "240", *offline_flags, "backend", "frontend"], environment, timeout=300)
            current["generation"] = str(directory)
            save_state(state_file, current)
            generations = sorted(root.glob(config["deployment"] + "-*"), key=lambda path: path.stat().st_mtime, reverse=True)
            for obsolete in generations[2:]:
                if obsolete.is_dir() and not obsolete.is_symlink():
                    shutil.rmtree(obsolete)
            print("Pilot deployment completed; credentials were not printed")
            return True
        finally:
            if (directory / "bootstrap").exists():
                shutil.rmtree(directory / "bootstrap")
            if not started:
                shutil.rmtree(directory)


def main():
    parser = argparse.ArgumentParser(description="Fullstack Starter Doppler file-delivery pilot")
    parser.add_argument("action", choices=("check", "deploy", "bootstrap", "metrics", "export", "recover", "poll"))
    parser.add_argument("config", type=Path)
    parser.add_argument("--confirm-database-role-changes", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--identity", type=Path)
    arguments = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    config = None
    try:
        if arguments.action in ("deploy", "bootstrap", "export", "recover", "poll"):
            information = arguments.config.lstat()
            if not stat.S_ISREG(information.st_mode) or information.st_uid != 0 or information.st_mode & 0o022:
                raise PilotError("Deployment configuration must be a root-owned regular file, not writable by other users")
        config = validate_config(json.loads(arguments.config.read_text()))
        if arguments.action not in ("check", "metrics") and config.get("enabled") is not True:
            raise PilotError("Enable this deployment explicitly in the protected host manifest before activation")
        if arguments.action == "check":
            print("Pilot configuration is valid; no credentials were read")
        elif arguments.action == "metrics":
            write_metrics(config, "inventory", inventory_metrics(config))
        elif arguments.action == "export":
            directory = private_directory("/run/prodenv-secrets")
            if quiet_run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(directory)]).strip() != "tmpfs":
                raise PilotError("Export retrieval requires tmpfs-backed runtime storage")
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                values = fetch_credentials(config, temporary)
                export_recovery(config, values)
                record_export(config, True)
                write_metrics(config, "inventory", inventory_metrics(config))
        elif arguments.action == "recover":
            if not arguments.snapshot or not arguments.identity:
                raise PilotError("Offline recovery requires a snapshot and a private identity file")
            values = recover_values(config, arguments.snapshot, arguments.identity)
            deploy(config, bootstrap=arguments.confirm_database_role_changes, recovered=values)
            record_delivery(config, True)
        else:
            if arguments.action == "bootstrap" and not arguments.confirm_database_role_changes:
                raise PilotError("Bootstrap changes database roles and stops consumers; explicit confirmation is required")
            write_metrics(config, "inventory", inventory_metrics(config))
            deploy(config, bootstrap=arguments.action == "bootstrap", only_if_changed=arguments.action == "poll")
            record_delivery(config, True)
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as error:
        if config and arguments.action in ("deploy", "bootstrap", "recover", "poll"):
            try:
                record_delivery(config, False)
            except (PilotError, OSError, ValueError):
                pass
        if config and arguments.action == "export":
            try:
                record_export(config, False)
            except (PilotError, OSError, ValueError):
                pass
        message = str(error) if isinstance(error, PilotError) else "Check configuration and permissions; secret-bearing details suppressed"
        print("Doppler pilot failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())