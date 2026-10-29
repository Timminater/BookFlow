Release builds place the versioned Windows x64 EXE, `.exe.sha256` and
`.exe.spdx.json` here before building both Docker images. Binaries are not
committed. Local builds without these files retain the manual setup flow.

The image stores them in `/opt/bookflow/kobo-patcher`. At application startup,
verified files seed `/config/kobopatcher` without overwriting an existing
EXE. Downloads are served from that persistent directory and validated on each
request. Upload all three matching files when replacing the current version.
