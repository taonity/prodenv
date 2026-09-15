# Backrest secrets

Create these untracked files before starting the stack:

- `admin_password`: Backrest UI password for `BACKREST_ADMIN_USERNAME`.
- `repository_password`: a unique restic repository encryption password.
- `aws_credentials`: standard shared AWS credentials file for the S3 provider.

Create each file from its `.example` template in this directory.

For Cloudflare R2 credentials, open Cloudflare Dashboard, select **R2 Object
Storage**, then **Manage R2 API Tokens** and create an account API token with
Object Read & Write access restricted to the backup bucket. Copy the displayed
Access Key ID and Secret Access Key into `aws_credentials`. The secret is shown
only once.

Store copies of the repository password and provider recovery credentials in a
password manager. Restrict all three files to the deployment user and never
commit them.