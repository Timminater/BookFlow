# Deployment Operations

BookFlow is deployed from a project-owned image pinned to an immutable
digest. It intentionally does not fall back to an upstream repository or a
floating `latest` tag.

## First deployment

For the standard compose file, create `.env` next to the downloaded compose
file. Keep this file out of source control and restrict it to the account that
operates Docker.

```dotenv
BOOKFLOW_IMAGE=ghcr.io/timminater/bookflow@sha256:<published-release-digest>
```

Use `BOOKFLOW_LITE_IMAGE` with an equivalent digest when using
`docker-compose.lite.yml`. Replace the placeholder with the complete digest
published for the release you reviewed. A tag is not a deployment pin: record
the tag as release metadata if useful, but deploy the corresponding digest.

Validate interpolation before starting:

```bash
docker compose --env-file .env -f docker-compose.yml config
docker compose --env-file .env -f docker-compose.yml up -d
```

The examples assume that `docker-compose.yml` and `.env` are in the same
directory. When running the repository copy, use `-f compose/docker-compose.yml`.

## Unraid layout and permissions

In Unraid, keep the configuration on the protected appdata share, for example
`/mnt/user/appdata/bookflow`, and mount it at `/config`. Mount the library or
ingest directory separately at `/books`.

- Run one BookFlow instance as the writer for its `/config` directory and book
  import path. Other library services should consume the completed files rather
  than sharing BookFlow's SQLite database.
- Set the Unraid Docker template or compose volume mappings to paths owned by
  the configured `PUID` and `PGID`; do not make the configuration directory
  world-writable.
- Keep WireGuard configuration and provider credentials under `/config` with
  the same restricted access. Do not put them in a Git repository or a support
  paste.
- Keep `/config/backups`, `/config/.restore-staging` and
  `/config/kobopatcher` on the same protected appdata mapping. Do not map
  those paths to public shares. The application creates backup archives with
  owner-only permissions and verifies patcher checksums before serving them.
- A staged restore is applied only during container startup, before database
  initialization. Restart BookFlow deliberately after staging and retain the
  automatically created pre-restore safety backup until the restored instance
  has been validated.

## Backup before an upgrade

Take a consistent backup before changing the image digest or applying a schema
migration. The simplest safe procedure stops the service first, which avoids a
SQLite WAL snapshot split.

```bash
docker compose --env-file .env down
tar -C /mnt/user/appdata -czf bookflow-config-$(date +%F-%H%M%S).tgz bookflow
docker compose --env-file .env up -d
```

Also retain the prior `.env` value (the exact image digest) with the backup.
For migration-specific database backup and recovery details, see
[Database migrations](dev/database-migrations.md). Test restoring a copy on a
non-production path before relying on a backup.

The repository also executes this contract in
`tests/core/test_migration_restore_rehearsal.py`: it migrates a populated
database, verifies the manifest checksum, restores the exact backup artifact
to an isolated path, and checks integrity, row preservation and pre-upgrade
schema. Run that test against every release candidate in addition to the
environment-specific operator rehearsal.

## Rollback

Never run an older image against a database that a newer image has migrated.
To roll back safely:

1. Stop BookFlow with `docker compose --env-file .env down`.
2. Set `BOOKFLOW_IMAGE` back to the exact previous digest.
3. Restore the matching pre-upgrade `/config` backup, including the SQLite
   database and its sidecar files if present.
4. Run `docker compose --env-file .env config`, then start the service and
   verify login, library access, and one download path.

If the restored configuration includes changed external credentials, rotate
them before re-enabling the service.

## Secret migration and rotation

Treat all existing provider API keys, download-client passwords, OIDC client
secrets, proxy credentials, and WireGuard keys as secrets.
Move them from plaintext templates or copied configuration into a restricted
Unraid Docker template, Docker secret mechanism, or a root/operator-readable
`.env` file. Keep only non-secret image pins in the deployment `.env` where
possible.

For each secret, create a replacement first, update the deployment, restart
and verify the affected integration, then revoke the old value. Rotate any
credentials that were previously committed, shared in compose files, or exposed
in diagnostic output. The entrypoint no longer emits the full environment in
debug mode, but application and proxy logs still require normal access control.

## Upgrade record

For every deployment change, record the date, image digest, source commit,
configuration backup name, migration outcome, and validation result. This is
the minimum evidence needed to reproduce a known-good release or perform a
safe rollback.

The release security gate and trust-boundary review are documented in
[Security threat model](security-threat-model.md).
