# Backrest secrets

Create these untracked files before starting the stack:

- `admin_password`: Backrest UI password for `BACKREST_ADMIN_USERNAME`.
- `repository_password`: a unique restic repository encryption password.
- `aws_credentials`: standard shared AWS credentials file for the S3 provider.

Store copies of the repository password and provider recovery credentials in a
password manager. Restrict all three files to the deployment user and never
commit them.