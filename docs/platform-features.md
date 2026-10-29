# Catalog, monitoring, Kobo patcher and operations

## Shared catalog and reading progress

`/` shows the deduplicated books most recently added by any user. `/catalog`
searches title, subtitle, author, series, ISBN and description through SQLite
FTS5. Catalog metadata is shared; personal files, downloads, Kobo shelves and
reading progress remain entitlement-scoped.

Only the current reading state is stored. Progress is monotonic between 0 and
100 percent unless a user explicitly resets it in BookFlow. A completed book
is removed from "currently reading". Paired Kobo devices exchange the same
state at `GET/PUT /api/kobo/<token>/v1/library/<book-id>/state`.

## Author and series monitors

Open Library declares author-monitor support. Hardcover declares author and
series support and requires its normal API configuration. Scans are durable,
retry after process restarts, run daily with jitter, and deduplicate provider
books, requests and request processes. `notify`, `request`, and authorized
`auto_acquire` actions are supported.

## Windows Kobo patcher

The Windows app opens with a four-step wizard: BookFlow account login,
USB reader selection, confirmed pairing/patching, and safe eject/restart/sync.
Password login uses the normal BookFlow HTTPS login endpoint and keeps cookies
only in memory. External-login users can still paste a temporary pairing code.
The wizard creates its own code after confirmation, so password users do not
need to copy tokens or codes. See [Kobo setup and operations](kobo.md).

The wizard or Kobo settings screen creates a ten-minute, single-use `SM1` pairing code.
The code contains only the BookFlow origin, random pairing identity/nonce,
expiry and protocol version. The server validates its HMAC and stores only a
nonce hash. Claim attempts are rate-limited per code identity and source IP.
Public origins require HTTPS; loopback/private-LAN HTTP requires an explicit
warning in the patcher. Completing a claim is bound to the exact origin the
user confirmed, and a failed patch revokes the newly issued Kobo token.

`BookFlowKoboPatcher-<version>-win-x64.exe` is built with Python 3.12,
Tkinter and PyInstaller `--onefile --windowed --noupx`. The release includes a
SHA-256 file and SPDX SBOM; tagged builds are Authenticode-signed when the
repository signing secrets are configured. The Windows release job rejects a
non-64-bit Python build and inspects the generated executable for the bundled
Python 3.12/Tk runtime before publishing it.

The patcher:

- detects Kobo volumes by the required `.kobo` structure without assuming a
  drive letter;
- requires an explicit selection when more than one Kobo is connected and
  shows volume, firmware/platform, model and a masked serial;
- never opens `KoboReader.sqlite`;
- changes only `api_endpoint` in `[OneStoreServices]` while preserving encoding,
  BOM, comments, order and line endings;
- creates an immutable original backup and timestamped safety backups;
- can restore a selected full backup offline;
- can restore only `https://storeapi.kobo.com` when no backup exists;
- verifies every write and offers a user-triggered safe-eject action.

The download manifest exposes protocol, release, size and SHA-256. Cached
release assets are rechecked against their checksum and SPDX SBOM before every
download. If no compatible artifact exists, the Reader screen shows the
manual `api_endpoint` instructions instead of offering an unverified file.

Production Kobo endpoints end at `/api/kobo/<token>`. The older `sync_url`
response is retained as a deprecated compatibility field.

## Web push

Configure:

| Variable | Purpose |
| --- | --- |
| `VAPID_PUBLIC_KEY` | URL-safe public VAPID key exposed to authenticated browsers |
| `VAPID_PRIVATE_KEY` | Private VAPID key or PEM used only by the server |
| `VAPID_SUBJECT` | `mailto:` or HTTPS VAPID contact |

Users may register multiple HTTPS (or loopback) browser endpoints, send a test
notification and filter
monitor, request, availability, request-process, metadata-report and backup events.
Delivery uses durable jobs. Endpoints returning HTTP 404 or 410 are retired.
Subscription key material is never returned by list routes or put in jobs/logs.

## Metadata governance

Users can report incorrect fields but cannot modify catalog metadata. Admins
review a queue, preview field-level diffs, apply changes with a source and
reason, compare the current snapshot with stored provider candidates (including
cover previews), inspect append-only revisions and field-provenance events, and
revert a prior revision.

## Backup and restore

The admin operations screen creates consistent SQLite backups plus configuration
files and a checksummed manifest. Ebook files, caches, logs and temporary files
are excluded. The default directory is `/config/backups`; seven daily recovery
points and four older weekly representatives are retained.
BookFlow schedules a durable daily backup with jitter in addition to manual
creation. Backup failures can trigger a personal web-push rule.

Set `BACKUP_ENCRYPTION_KEY` to enable AES-GCM archive encryption. Restores are
verified and extracted into `/config/.restore-staging`. A restore is applied
only before database initialization on the next container start, after making a
complete `backups/restore-safety-*.zip` recovery archive of the current
database and configuration. If applying the staged restore fails, BookFlow
rolls both the database and changed configuration files back and retains the
staging directory, marker and safety archive for diagnosis or retry.

## PWA

BookFlow includes base-path-aware install metadata, 192/512/maskable icons,
an install/update prompt and an offline shell. Build-time generated hashes add
only the versioned JavaScript/CSS application shell to the precache. The service worker treats API,
authentication, download, pairing, Kobo and token traffic as network-only and
does not cache personal library data.

## Production acceptance checklist

The automated suite covers simulated FAT32, exFAT and NTFS-style volumes,
arbitrary drive letters, duplicate or malformed configuration sections,
read-only/write-failure paths, interrupted writes, rollback, both restore
flows, protocol replay/expiry, entitlement isolation and PWA cache policy.
Before publishing a production release, also record evidence for the checks
that require real external systems:

- patch, sync, restore and safe eject on at least one older and one current
  Kobo firmware version;
- the one-file EXE on a clean Windows x64 machine without Python installed,
  including antivirus reputation and Authenticode verification;
- VAPID delivery, retry and HTTP 410 retirement against a real push vendor;
- Hardcover and Open Library monitoring against configured production
  credentials and rate limits;
- an encrypted backup restore rehearsal in a disposable copy of the production
  configuration.
