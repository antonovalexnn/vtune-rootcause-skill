---
name: vtune-artifactory-build
description: >-
  Download a VTune daily build from Intel Artifactory by build number
  (e.g. 632837) to the local machine or to a remote Linux or Windows box you
  supply at runtime — public installer package (default) or developer package,
  optionally extracted with bin64/vtune located. Runs the download on the
  remote itself over ssh. Use standalone or from the rootcause skill; acts
  without asking for permissions.
argument-hint: <BUILD-NUMBER> --dest <DIR> | --remote USER@HOST [--remote-os windows]
allowed-tools:
  - Read
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/vtune-artifactory-build/vab_fetch.py" *)
disallowed-tools:
  - AskUserQuestion
---

# vtune-artifactory-build — fetch a VTune build from Artifactory by number

Given a build number, resolve and download that VTune daily build from Intel
Artifactory. Works two ways:

- **standalone** — the user gives a build number and a destination;
- **from `rootcause`** — pull a prebuilt VTune into the ticket's
  `jira_artifacts/<key>/build/` instead of building locally with scons.

Everything is done by one stdlib helper, **`vab_fetch.py`**. Never reimplement
the download by hand (no ad-hoc `curl`/`ssh`) — always call the helper.

## No permission prompts

The helper shells out to `curl`/`ssh`/`7z` internally, so a **single** grant —
`Bash(python "${CLAUDE_PLUGIN_ROOT}/skills/vtune-artifactory-build/vab_fetch.py" *)` —
covers every download. Run it directly; do not construct your own network
commands (those would prompt and are unnecessary).

## What's on Artifactory

```
https://<host>/artifactory/analyzerengineering-ba-local/Products/vtune/daily/<stream>/<build>/<platform>/<config>/build/
```
- `<host>` default `af01p-ba.devtools.intel.com` (anonymous intranet read — no token).
- `<stream>` default `master`; `<platform>` ∈ `windows | linux | linux-aarch64 | freebsd`;
  `<config>` ∈ `release | debug`.
- The `build/` folder holds the installer archives
  `Intel_VTune_Profiler_<ver>[_internal|_nda].{zip,tar.gz}` (the version varies per
  build, so the exact name is resolved by listing the folder) and
  `developer-package.7z`.

## Destination — always required (except `--list`)

Pass **exactly one**:
- `--dest DIR` — download locally; the build lands in `<DIR>/<build>/`.
- `--remote USER@HOST [--remote-dest DIR] [--remote-os linux|windows]` — run the
  download **on the remote box** over ssh; it lands in `<DIR>/<build>/` there.
  `--remote-dest` defaults to `~/vtune_builds` (linux) / `~\vtune_builds` (windows,
  → the user profile).

A local `--dest` must be given explicitly (no default). For a metadata-only
lookup use `--list` (no destination needed).

### Remote OS

- `--remote-os linux` *(default)* — runs `curl`→`wget` + `tar`/`unzip`/`7z` via
  a `bash -s` script over ssh; `~` → `$HOME`.
- `--remote-os windows` — the box's ssh shell is cmd.exe, so the helper runs a
  PowerShell script (base64 `-EncodedCommand`, so nothing gets mangled) using
  `curl.exe` + `tar.exe`; `~` → `$env:USERPROFILE`. `.7z` needs 7-Zip on the
  remote PATH. Platform defaults to `windows` in this mode.

**Remote path caveat (Git-Bash, linux only):** give a linux `--remote-dest` as a
`~/…` or relative path (the default is safe). An **absolute** POSIX path like
`/tmp/foo` is silently rewritten to a Windows path (`C:\…`) by Git-Bash *before*
the script runs, so it can't work on the remote — the helper detects this and
errors with a hint.

## Package variants

- `--package public` *(default)* — the shipped installer (`Intel_VTune_Profiler_<ver>`).
- `--package developer` — `developer-package.7z` (full dev layout; large, ~4–5 GB).
- `--package internal` / `--package nda` — the internal / NDA installer builds.

## Remote credentials (per run, not stored)

- `--remote-password-env VAR` *(preferred)* — env var holding the ssh password;
  the helper feeds it via a throwaway `SSH_ASKPASS` shim (never on a command line).
- `--remote-password PW` — inline (avoid; prefer the env var).
- Omit both → assume key-based ssh auth.

Passwords are never printed or logged. The remote box must be able to reach
Artifactory (Intel intranet); if it can't, the helper reports it plainly (there
is no local-then-scp fallback by design).

## Command cookbook

| Intent | Command |
|--------|---------|
| Inspect a build (no download) | `python "${CLAUDE_PLUGIN_ROOT}/skills/vtune-artifactory-build/vab_fetch.py" <build> --list --platform windows` |
| Local, public, extracted | `python ".../vab_fetch.py" <build> --dest "<DIR>"` |
| Local, developer package | `python ".../vab_fetch.py" <build> --dest "<DIR>" --package developer` |
| Local, archive only | `python ".../vab_fetch.py" <build> --dest "<DIR>" --no-extract` |
| Remote Linux box (key auth) | `python ".../vab_fetch.py" <build> --remote USER@HOST` |
| Remote Linux with password | `RPW=... python ".../vab_fetch.py" <build> --remote USER@HOST --remote-password-env RPW` |
| Remote Windows box | `RPW=... python ".../vab_fetch.py" <build> --remote USER@HOST --remote-os windows --remote-password-env RPW` |
| Debug config / other stream | add `--config debug` and/or `--stream <name>` |

Platform default: `windows` for local and windows remotes, `linux` for a linux
remote — override with `--platform`.
Re-running is cheap: an already-downloaded build is detected and `SKIPPED=yes` is
printed; pass `--force` to re-download.

## Reading the output

The helper prints parse-friendly `KEY=value` lines. The ones you act on:
- `ARCHIVE_NAME`, `ARCHIVE_SIZE`, `DOWNLOAD_URL` — what was resolved.
- `LOCAL_ARCHIVE` / `REMOTE_ARCHIVE` — where the archive landed.
- `EXTRACTED_DIR` / `REMOTE_EXTRACTED_DIR` — the extraction tree (unless `--no-extract`).
- `VTUNE_BIN` — path to `bin64/vtune[.exe]` for installer packages (`(n/a)` for the
  developer package or if not found).
- `SKIPPED=yes|no` — whether the download was reused from cache.

Report these back to the user (or, under rootcause, record the build path in the
artifact folder). On failure the helper exits non-zero with a message naming the
URL or cause — surface it; do not retry with hand-built commands.

## Safety

- Write only under the destination the user gave (`--dest` / `--remote-dest`).
- Never print, log, or echo a remote password.
- Do not fabricate a build number — if the build/platform/package doesn't exist,
  the helper's 404 message tells you; relay it.
