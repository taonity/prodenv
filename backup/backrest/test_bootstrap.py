import copy
import json
import unittest
from pathlib import Path

import bootstrap


class FakeClient:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.add_repo_calls = 0
        self.set_config_calls = 0

    def get_config(self):
        return copy.deepcopy(self.config)

    def set_config(self, config):
        self.set_config_calls += 1
        self.config = copy.deepcopy(config)
        self.config["modno"] = self.config.get("modno", 0) + 1
        return self.get_config()

    def add_repo(self, repository):
        self.add_repo_calls += 1
        stored = copy.deepcopy(repository)
        stored.pop("password", None)
        stored["env"] = sorted(stored.get("env", []))
        stored["guid"] = "repository-guid"
        repositories = self.config.setdefault("repos", [])
        for index, current in enumerate(repositories):
            if current["id"] == stored["id"]:
                repositories[index] = stored
                break
        else:
            repositories.append(stored)
        return self.get_config()

    def hash_password(self, password):
        return f"bcrypt:{password}"


def load_desired():
    path = Path(__file__).with_name("desired.json")
    return json.loads(path.read_text(encoding="utf-8"))


def reconcile(client):
    desired = load_desired()
    settings = {
        "repository_id": "offsite",
        "repository_uri": "/tmp/repository",
        "admin_username": "admin",
    }
    config = bootstrap.configure_instance(client, "prodenv")
    config = bootstrap.configure_repository(client, config, desired, settings)
    return bootstrap.configure_plans_and_auth(
        client, config, desired, settings, "admin-password"
    )


class BootstrapTest(unittest.TestCase):
    def test_first_run_and_repeat_run_are_idempotent(self):
        client = FakeClient(
            {
                "modno": 0,
                "instance": "",
                "repos": [],
                "plans": [],
                "auth": {"disabled": True, "users": []},
            }
        )

        reconcile(client)

        self.assertEqual(client.config["instance"], "prodenv")
        self.assertEqual(client.add_repo_calls, 1)
        self.assertEqual(client.set_config_calls, 2)
        self.assertFalse(client.config["auth"]["disabled"])
        self.assertEqual(client.config["auth"]["users"][0]["name"], "admin")
        self.assertEqual(client.config["plans"][0]["repo"], "offsite")

        reconcile(client)

        self.assertEqual(client.add_repo_calls, 1)
        self.assertEqual(client.set_config_calls, 2)

    def test_unknown_managed_plan_metadata_is_preserved(self):
        desired_plan = copy.deepcopy(load_desired()["plans"][0])
        desired_plan["repo"] = "offsite"
        existing_plan = copy.deepcopy(desired_plan)
        existing_plan["futureBackrestField"] = {"value": "keep-me"}
        client = FakeClient(
            {
                "instance": "prodenv",
                "repos": [
                    {
                        "id": "offsite",
                        "uri": "/tmp/repository",
                        "guid": "repository-guid",
                        "env": [
                            "AWS_SHARED_CREDENTIALS_FILE=/run/secrets/backrest_aws_credentials",
                            "RESTIC_PASSWORD_FILE=/run/secrets/backrest_repository_password",
                        ],
                        "autoUnlock": True,
                        "prunePolicy": load_desired()["repository"]["prunePolicy"],
                        "checkPolicy": load_desired()["repository"]["checkPolicy"],
                    }
                ],
                "plans": [existing_plan],
                "auth": {
                    "disabled": False,
                    "users": [{"name": "admin", "passwordBcrypt": "********"}],
                },
            }
        )

        reconcile(client)

        self.assertEqual(client.set_config_calls, 0)
        self.assertEqual(
            client.config["plans"][0]["futureBackrestField"], {"value": "keep-me"}
        )

    def test_unmanaged_resources_are_preserved(self):
        client = FakeClient(
            {
                "modno": 4,
                "instance": "prodenv",
                "repos": [{"id": "manual", "uri": "/tmp/manual"}],
                "plans": [{"id": "manual-plan", "repo": "manual", "paths": ["/manual"]}],
                "auth": {
                    "disabled": False,
                    "users": [{"name": "operator", "passwordBcrypt": "********"}],
                },
            }
        )

        reconcile(client)

        self.assertTrue(any(repo["id"] == "manual" for repo in client.config["repos"]))
        self.assertTrue(
            any(plan["id"] == "manual-plan" for plan in client.config["plans"])
        )
        self.assertTrue(
            any(user["name"] == "operator" for user in client.config["auth"]["users"])
        )

    def test_existing_instance_is_never_renamed(self):
        client = FakeClient({"instance": "another-instance"})

        with self.assertRaisesRegex(bootstrap.BackrestError, "refusing to rename"):
            bootstrap.configure_instance(client, "prodenv")


if __name__ == "__main__":
    unittest.main()