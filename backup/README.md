# Backups

Backrest manages encrypted off-site restic snapshots. Configuration is applied
automatically by `backrest-bootstrap` whenever the stack starts.

## Setup

1. Create `backrest/.env` from `backrest/.env.example` and set the repository URI.
2. Create the untracked files described in
   [backrest/secrets/README.md](backrest/secrets/README.md).
3. Store recovery credentials separately in a password manager.
4. Start the normal Compose stack and confirm `backrest-bootstrap` succeeds.

The UI is available only on `127.0.0.1:9898`. Use an SSH tunnel for remote
access.

## Project exports

Application databases must create database-native exports in the external
`prodenv-backup-staging` volume. Backing up a live database volume is
intentionally unsupported.

Use [scripts/export-postgres.sh](scripts/export-postgres.sh) for PostgreSQL.
Mount the staging volume in each project's export job and schedule exports
before the snapshot time defined in
[backrest/desired.json](backrest/desired.json). Backrest rejects the entire
snapshot when any project export is missing, empty, in progress, or older than
26 hours.

For PostgreSQL systems requiring recovery points more frequent than daily,
configure pgBackRest and WAL archiving in the application's database stack.
Keep the logical export as the portable server-migration path.

## Restore verification

Follow these steps on the production Docker host. Never restore over production
data.

1. Open Backrest in the browser.

2. Open **Plans**, then click **all-project-exports**.

3. Click a successful backup, then expand **Snapshot Browser**.

4. Expand **userdata**, expand the project directory such as
   **fullstack-starter-stage**, and find **postgres**.

5. Open the action menu next to **postgres** and click **Restore to path**.

6. In the **Restore to path** field, enter:

   ```text
   /restore/stage-restore-20260920
   ```

   Do not create this directory yourself. If it was used before, change the
   date or add the current time to make a new path.

7. Click **Restore**, confirm the action, and wait for the restore operation to
   report success.

8. Open a shell on the Docker host and change to the `prodenv` repository
   directory.

9. Verify the restored checksums:

   ```sh
   sh backup/scripts/verify-postgres-restore.sh stage-restore-20260920
   ```

10. Import the dump into a temporary PostgreSQL database and validate it:

   ```sh
   sh backup/scripts/validate-postgres-restore.sh stage-restore-20260920
   ```

   This verifies the checksums again, imports the dump into a temporary
   network-isolated PostgreSQL container, confirms that application tables
   exist, and removes the container.

11. Record the successful result. Run an application smoke test when a full
    test environment is available.

The scripts use the `prodenv-backup-restore` volume and `postgres:16` image by
default. Override them when required:

```sh
RESTORE_VOLUME=other-volume POSTGRES_IMAGE=postgres:17 \
   sh backup/scripts/validate-postgres-restore.sh stage-restore-20260920
```

Test restores quarterly and perform a complete recovery drill yearly.

## Database recovery

The verification procedure above does not change an application database. Use
the following procedures only after the selected backup passes verification.
Both procedures delete the target database before importing the dump, so stop
the target application and all jobs that write to it first.

### Roll back the same environment

Use rollback mode when restoring to the PostgreSQL cluster from which the dump
was created. Existing cluster roles are kept, while the target database is
deleted and recreated from the dump with its original ownership and grants.

1. Stop the application's backend, Flyway, exporters, and other database
    clients.
2. Set the target Compose network, database service, and administrator
   credentials. Enter the password when prompted so it is not saved in shell
   history:

   ```sh
   export DOCKER_NETWORK=fullstack-starter-stage_backend
   export PGHOST=db
   export PGUSER=dbadmin
   read -r -s PGPASSWORD && export PGPASSWORD
   ```

3. Replace the database from the restored directory:

    ```sh
    REPLACE_DATABASE=fullstack_starter_db \
     sh backup/scripts/replace-postgres-restore.sh \
     stage-restore-20260920 \
       fullstack_starter_db rollback
    ```

4. Start the application and run a smoke test.

This restores the database schema and data to the state captured by `pg_dump`.
It does not roll back PostgreSQL cluster roles or server configuration.

### Move data to a reset or new environment

Use migration mode when source and target role names may differ. Initialize the
target environment once so its administrator and application roles exist, then
stop its application services. Migration mode deletes and recreates the target
database, imports without source ownership or grants, and grants access to the
target application role.

```sh
REPLACE_DATABASE=fullstack_starter_db \
TARGET_APP_USER=app \
   sh backup/scripts/replace-postgres-restore.sh \
   stage-restore-20260920 \
   fullstack_starter_db migration
```

Start the target application and run a smoke test after the command succeeds.
Do not import `globals.sql` for routine rollback or migration: it contains
cluster-wide roles and memberships and could alter unrelated databases. Keep it
for a separately reviewed whole-cluster disaster recovery.

Loki already enforces 30-day retention. Operational logs are intentionally not
included in long-lived snapshots unless a separate audit requirement exists.