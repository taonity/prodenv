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

Use [scripts/export-postgres.sh](scripts/export-postgres.sh) for PostgreSQL or
[scripts/export-mysql.sh](scripts/export-mysql.sh) for MySQL/MariaDB. Mount the
staging volume in each project's export job and schedule exports before the
snapshot time defined in
[backrest/desired.json](backrest/desired.json). Backrest rejects the entire
snapshot when any project export is missing, empty, in progress, or older than
26 hours.

For PostgreSQL systems requiring recovery points more frequent than daily,
configure pgBackRest and WAL archiving in the application's database stack.
Keep the logical export as the portable server-migration path.

## Restore verification

Restore into `/restore`, never over production data. Verify the included
checksums, import into an isolated database, and run an application smoke test.
Test restores quarterly and perform a complete recovery drill yearly.

Loki already enforces 30-day retention. Operational logs are intentionally not
included in long-lived snapshots unless a separate audit requirement exists.