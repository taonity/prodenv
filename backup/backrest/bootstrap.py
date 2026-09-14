#!/usr/bin/env python3

import base64
import copy
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


class BackrestError(RuntimeError):
    pass


class BackrestClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        credentials = f"{username}:{password}".encode()
        self.authorization = "Basic " + base64.b64encode(credentials).decode()

    def call(self, service, method, payload):
        request = urllib.request.Request(
            f"{self.base_url}/{service}/{method}",
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
            raise BackrestError(
                f"{service}/{method} failed with HTTP {error.code}: {body}"
            ) from error
        except urllib.error.URLError as error:
            raise BackrestError(f"Cannot reach Backrest: {error.reason}") from error
        return json.loads(body) if body else {}

    def get_config(self):
        return self.call("v1.Backrest", "GetConfig", {})

    def set_config(self, config):
        return self.call("v1.Backrest", "SetConfig", config)

    def add_repo(self, repository):
        return self.call("v1.Backrest", "AddRepo", {"repo": repository})

    def hash_password(self, password):
        response = self.call("v1.Authentication", "HashPassword", {"value": password})
        return response["value"]


def require_environment(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise BackrestError(f"Required environment variable {name} is empty")
    return value


def read_secret(path):
    try:
        value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
        raise BackrestError(f"Cannot read secret {path}: {error}") from error
    if not value:
        raise BackrestError(f"Secret {path} is empty")
    return value


def contains_desired(actual, desired):
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains_desired(actual[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return actual == desired
    return actual == desired


def upsert_managed_plan(items, desired):
    managed_keys = {
        "id",
        "repo",
        "paths",
        "excludes",
        "iexcludes",
        "schedule",
        "retention",
        "hooks",
        "backup_flags",
        "skipIfUnchanged",
    }
    for index, current in enumerate(items):
        if current.get("id") != desired["id"]:
            continue
        reconciled = {
            key: copy.deepcopy(value)
            for key, value in current.items()
            if key not in managed_keys
        }
        reconciled.update(copy.deepcopy(desired))
        if current == reconciled:
            return False
        items[index] = reconciled
        return True
    items.append(copy.deepcopy(desired))
    return True


def configure_instance(client, desired_instance):
    config = client.get_config()
    current_instance = config.get("instance", "")
    if current_instance and current_instance != desired_instance:
        raise BackrestError(
            f"Backrest instance is {current_instance!r}, expected {desired_instance!r}; "
            "refusing to rename an existing instance"
        )
    if not current_instance:
        config["instance"] = desired_instance
        config = client.set_config(config)
        print(f"Configured Backrest instance {desired_instance!r}.")
    return config


def configure_repository(client, config, desired, settings):
    repository = copy.deepcopy(desired["repository"])
    repository.update(
        {
            "id": settings["repository_id"],
            "uri": settings["repository_uri"],
            "env": [
                "AWS_SHARED_CREDENTIALS_FILE=/run/secrets/backrest_aws_credentials",
                "RESTIC_PASSWORD_FILE=/run/secrets/backrest_repository_password",
            ],
        }
    )
    current = next(
        (item for item in config.get("repos", []) if item.get("id") == repository["id"]),
        None,
    )
    if current is None or not contains_desired(current, repository):
        config = client.add_repo(repository)
        print(f"Configured repository {repository['id']!r}.")
    return config


def configure_plans_and_auth(client, config, desired, settings, admin_password):
    changed = False
    plans = config.setdefault("plans", [])
    for plan_template in desired["plans"]:
        plan = copy.deepcopy(plan_template)
        plan["repo"] = settings["repository_id"]
        changed = upsert_managed_plan(plans, plan) or changed

    auth = config.setdefault("auth", {})
    users = auth.setdefault("users", [])
    admin = next(
        (user for user in users if user.get("name") == settings["admin_username"]),
        None,
    )
    if admin is None:
        users.append(
            {
                "name": settings["admin_username"],
                "passwordBcrypt": client.hash_password(admin_password),
            }
        )
        changed = True
    if auth.get("disabled", True):
        auth["disabled"] = False
        changed = True

    if changed:
        config = client.set_config(config)
        print("Reconciled managed plans and authentication.")
    return config


def validate_desired(desired):
    if not isinstance(desired.get("repository"), dict):
        raise BackrestError("Desired config must contain a repository object")
    plans = desired.get("plans")
    if not isinstance(plans, list) or not plans:
        raise BackrestError("Desired config must contain at least one plan")
    plan_ids = [plan.get("id") for plan in plans]
    if any(not plan_id for plan_id in plan_ids) or len(plan_ids) != len(set(plan_ids)):
        raise BackrestError("Every managed plan must have a unique non-empty id")


def main():
    desired_path = os.environ.get("BACKREST_DESIRED_CONFIG", "/bootstrap/desired.json")
    try:
        desired = json.loads(Path(desired_path).read_text(encoding="utf-8"))
        validate_desired(desired)
        settings = {
            "repository_id": require_environment("BACKREST_REPOSITORY_ID"),
            "repository_uri": require_environment("BACKREST_REPOSITORY_URI"),
            "admin_username": require_environment("BACKREST_ADMIN_USERNAME"),
        }
        desired_instance = require_environment("BACKREST_INSTANCE")
        admin_password = read_secret(
            os.environ.get(
                "BACKREST_ADMIN_PASSWORD_FILE",
                "/run/secrets/backrest_admin_password",
            )
        )
        client = BackrestClient(
            os.environ.get("BACKREST_URL", "http://backrest:9898"),
            settings["admin_username"],
            admin_password,
        )
        config = configure_instance(client, desired_instance)
        config = configure_repository(client, config, desired, settings)
        configure_plans_and_auth(client, config, desired, settings, admin_password)
        print("Backrest bootstrap completed successfully.")
    except (BackrestError, KeyError, json.JSONDecodeError) as error:
        print(f"Backrest bootstrap failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())