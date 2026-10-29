# Kobo setup and operations

## Windows wizard

Download the Windows patcher from **My settings → Kobo**. It includes Python
and Tk, so users do not need to install a runtime.

1. Enter the HTTPS address of your BookFlow instance and sign in with your own
   username/password. The app does not save passwords or login cookies to disk.
   Accounts that only use external login can instead create a temporary pairing
   code on the website and select the wizard's code option.
2. Connect the Kobo via USB, choose **Connect** on the reader, and select the
   intended reader. If several devices are attached, selection is mandatory.
3. Review the account, server and device, then confirm. The wizard makes a
   configuration backup, creates the account's Kobo token, writes `api_endpoint`
   and verifies the result. A failed write attempts rollback and revokes the new
   token. BookFlow currently permits one active reader token per user: a new
   pairing replaces that user's previous token.
4. Safely eject, disconnect, restart the reader, connect to Wi-Fi and synchronize.
   Books must be present in that account's Kobo-sync list.

The official Kobo Store/OverDrive synchronization changes when using BookFlow.
Existing files and `KoboReader.sqlite` are not edited by the patcher. The
offline restore option can restore a saved configuration or the official Kobo
endpoint without an account login.

## Docker and Unraid

Both release images include the tested patcher, checksum and SPDX metadata at
`/opt/bookflow/kobo-patcher`. On application startup, BookFlow copies missing
version-matched assets into **`/config/kobopatcher`**. Existing replacements are
preserved. Downloads are served from this persistent directory, with checksum
and protocol/version verification for every request. The volume works without
GitHub access. A matching GitHub release is a fallback for unbundled builds.

With `/mnt/user/Docker/bookflow/config` mounted at `/config`, the host directory is:

```
/mnt/user/Docker/bookflow/config/kobopatcher
```

To replace the current patcher without rebuilding the container, upload the
complete set for the running BookFlow release:

```
BookFlowKoboPatcher-1.2.9-win-x64.exe
BookFlowKoboPatcher-1.2.9-win-x64.exe.sha256
BookFlowKoboPatcher-1.2.9-win-x64.exe.spdx.json
```

Keep the version and protocol metadata consistent and update the checksum when
rebuilding the EXE. Stage replacement files outside the download directory and
replace the sidecars and EXE together; an incomplete set is not served. Refresh
the Kobo settings page afterwards. No container restart is needed. New releases
seed their own version, preserving old files for operator-managed rollback.

The release pipeline builds/tests the Windows app first and then bundles those
same artifacts into both Docker images. It uploads the EXE and sidecars to a
draft GitHub release; publish that release after all image jobs pass.

## Device protocol and validation

The token endpoint is a Kobo Store API base URL, not an OPDS feed. Initialization
returns `Resources`; auth/refresh requests use the reader's JSON contract; sync
returns entitlement arrays and a signed `X-Kobo-Synctoken`. Completed syncs retain
their checkpoint across server restarts and later library changes. Metadata,
download links, covers, removal and reading-state requests check the token owner.
Only EPUB/KEPUB files are advertised to this device API. The synthetic internal
change journal is never sent directly to the reader.

Protocol field names were checked against the independent
[Calibre-Web implementation](https://github.com/janeczku/calibre-web/blob/master/cps/kobo.py).
Automated tests exercise initialization, auth, paginated/delta sync, download,
bookmarks, cross-user isolation, the production browser-origin guard and patcher
login/rollback. These are distinct from physical firmware acceptance.

### September 2026 compatibility review

Compared against [Calibre-Web](https://github.com/janeczku/calibre-web/blob/master/cps/kobo.py)
and [Grimmory's Kobo services](https://github.com/grimmory-tools/grimmory/tree/develop/backend/src/main/java/org/booklore/service/kobo).
These independently implement the unofficial device API; matching their wire
shapes does not replace testing on physical firmware.

| Area | BookFlow behavior |
| --- | --- |
| Bootstrap | Explicit OneStore flags, library-book resource and authenticated affiliate/assets/deals/subscription compatibility routes. |
| Authentication | Personal revocable token, device authentication and refresh, local OAuth host and OIDC discovery/token bootstrap; no forwarding of credentials to Kobo. |
| Library | Stable book IDs, signed paginated delta cursor, metadata/downloads, covers and deletion tombstones. |
| Reading progress | Newest reading timestamp wins in both directions, including intentional backwards moves; stale offline writes cannot replace newer reading. |
| Web resume | Server timestamps are compared with local saved positions; Kobo updates clear obsolete web CFIs or replace them with translated CFIs. |
| Sentence positions | The pinned kepubify converter exports a separate cached KEPUB. Original EPUB CFIs map to real KoboSpan IDs and back, only when chapter text matches. Unsupported locators/editions retain percentage-only progress. |
| Collections | BookFlow has no collection model to synchronize; tags remain empty. |
| Store services | Purchases, subscriptions, wishlist and loyalty functionality are not implemented; optional device requests receive the explicit compatibility shapes. |

KEPUB metadata size and downloaded bytes come from the same derived file. The
original library EPUB and its file ID remain unchanged. Readers which already
downloaded the old EPUB may need to remove its local download and download the
book again to obtain sentence markers; do not delete their library entitlement.

Temporary converter output must end in `.kepub.epub`: kepubify treats a plain
`.tmp.epub` output as a copy operation. The conversion cache contract is versioned
to bypass older unchanged-EPUB results without deleting the original files.

Physical acceptance is pending for Marion's Kobo (reported firmware
4.38.23828). Test web-to-reader and reader-to-web after closing the book and
synchronizing, including a backwards move. Record the observed result;
automated tests alone are not proof of successful physical reader sync.

The first September 27 device retest still stopped after initialization despite
HTTP 200. The missing `oauth_host` bootstrap was then added based on
[Calibre-Web-Automated #1418](https://github.com/crocodilestick/Calibre-Web-Automated/issues/1418)
and [BookOrbit #766](https://github.com/bookorbit/bookorbit/issues/766).
Discovery returns endpoint configuration, not a token response; all identity
compatibility endpoints remain protected by the personal URL token.
