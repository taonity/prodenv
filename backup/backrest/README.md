# Automatic Backrest configuration

The `backrest-bootstrap` service reconciles Backrest after it becomes healthy.
It preserves generated identity, operation history, and unmanaged resources.

Edit [desired.json](desired.json) to maintain repository checks, pruning,
backup schedules, retention, and hooks. Restarting the stack applies changes
idempotently.

## Operations

- Set deployment values in `.env` and secrets in [secrets](secrets/README.md).
- Confirm `backrest-bootstrap` exits successfully after deployment.
- Keep the `backrest-config` volume during routine updates.
- Do not rename an initialized instance through `.env`.
- Rotate existing admin and repository passwords with Backrest/restic before
	updating their secret files.

Recovery requires the restic password and object-storage credentials stored
outside this server.